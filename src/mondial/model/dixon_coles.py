"""Dixon-Coles bivariate Poisson goal model.

Reference: Dixon & Coles (1997), "Modelling Association Football Scores and
Inefficiencies in the Football Betting Market", JRSS-C 46(2).

Per-match goal rates (ARCHITECTURE.md §6.1):

    log lambda_home = mu + alpha[home] + beta[away]  + h + w . x_home
    log lambda_away = mu + alpha[away] + beta[home]      + w . x_away

with sum-to-zero identifiability constraints on alpha and beta. Following the
original DC paper, alpha is attack rate and beta is *defensive weakness*
(higher beta -> more goals conceded). x_away is the feature vector built from
the away team's perspective (typically the home-perspective vector with
direction features sign-flipped and confederation one-hots swapped).

Low-score correlation correction tau is applied to (0,0), (0,1), (1,0), (1,1).

Parameter layout (theta packed for L-BFGS-B):
    theta[0]                     mu          (global intercept)
    theta[1 : n]                 alpha_free  (n-1 free; alpha[n-1] = -sum(free))
    theta[n : 2n-1]              beta_free   (n-1 free; beta[n-1]  = -sum(free))
    theta[2n-1]                  home_adv
    theta[2n]                    rho
    theta[2n+1 : 2n+1+k]         feature_weights
"""
from __future__ import annotations

import warnings
from dataclasses import dataclass

import numpy as np
import pandas as pd
from scipy.optimize import minimize
from scipy.special import gammaln
from scipy.stats import poisson


@dataclass
class DCParams:
    mu: float                       # global intercept
    attack: dict[int, float]        # team_id -> attack strength (alpha)
    defence: dict[int, float]       # team_id -> defensive weakness (beta)
    home_adv: float
    rho: float                      # low-score correction
    feature_weights: np.ndarray     # one entry per feature in features.py

    @classmethod
    def from_json(cls, path=None) -> "DCParams":
        """Load fitted params from dc_params.json (written by pipelines.train.fit).

        Validates the persisted feature-weight count against features.py so a
        stale file fails loudly instead of producing wrong predictions.
        """
        import json
        from pathlib import Path

        from mondial.config import DATA_DIR
        from mondial.model import features as _feat

        path = Path(path) if path is not None else DATA_DIR / "dc_params.json"
        payload = json.loads(path.read_text())
        w = payload["feature_weights"]
        if len(w) != _feat.n_features():
            raise ValueError(
                f"{path.name} has {len(w)} feature weights but features.py "
                f"defines {_feat.n_features()} ({_feat.feature_names()}); "
                f"re-run `python -m mondial.pipelines.train --fit`."
            )
        return cls(
            mu=payload["mu"], home_adv=payload["home_adv"], rho=payload["rho"],
            feature_weights=np.array(w, dtype=float),
            attack={int(k): v for k, v in payload["attack"].items()},
            defence={int(k): v for k, v in payload["defence"].items()},
        )


def tau(x: int, y: int, lam: float, mu: float, rho: float) -> float:
    """Low-score correction term per Dixon-Coles. lam=home rate, mu=away rate."""
    if x == 0 and y == 0:
        return 1 - lam * mu * rho
    if x == 0 and y == 1:
        return 1 + lam * rho
    if x == 1 and y == 0:
        return 1 + mu * rho
    if x == 1 and y == 1:
        return 1 - rho
    return 1.0


def match_probs(
    lam_home: float, lam_away: float, rho: float, max_goals: int = 10
) -> tuple[float, float, float]:
    """Return (P(H), P(D), P(A)) by summing over the score grid."""
    h_pmf = poisson.pmf(np.arange(max_goals + 1), lam_home)
    a_pmf = poisson.pmf(np.arange(max_goals + 1), lam_away)
    grid = np.outer(h_pmf, a_pmf)
    for x in (0, 1):
        for y in (0, 1):
            grid[x, y] *= tau(x, y, lam_home, lam_away, rho)
    grid /= grid.sum()
    p_h = np.tril(grid, -1).sum()
    p_d = np.trace(grid)
    p_a = np.triu(grid, 1).sum()
    return float(p_h), float(p_d), float(p_a)


def _build_design(
    matches: pd.DataFrame, X_home: np.ndarray, X_away: np.ndarray
) -> dict:
    required = {"home_id", "away_id", "home_goals", "away_goals"}
    missing = required - set(matches.columns)
    if missing:
        raise ValueError(f"matches missing required columns: {sorted(missing)}")
    n_matches = len(matches)
    X_home = np.asarray(X_home, dtype=float)
    X_away = np.asarray(X_away, dtype=float)
    if X_home.ndim != 2 or X_away.ndim != 2:
        raise ValueError("X_home and X_away must be 2-D arrays")
    if X_home.shape[0] != n_matches or X_away.shape[0] != n_matches:
        raise ValueError(
            f"feature row count {X_home.shape[0]}/{X_away.shape[0]} != matches {n_matches}"
        )
    if X_home.shape[1] != X_away.shape[1]:
        raise ValueError("X_home and X_away must have the same number of columns")

    teams = sorted(set(matches["home_id"]).union(matches["away_id"]))
    team_idx = {t: i for i, t in enumerate(teams)}
    return {
        "teams": teams,
        "team_idx": team_idx,
        "home_idx": matches["home_id"].map(team_idx).to_numpy(),
        "away_idx": matches["away_id"].map(team_idx).to_numpy(),
        "home_goals": matches["home_goals"].to_numpy(dtype=float),
        "away_goals": matches["away_goals"].to_numpy(dtype=float),
        "X_home": X_home,
        "X_away": X_away,
        "n_teams": len(teams),
        "n_feat": X_home.shape[1],
    }


def _unpack_theta(theta: np.ndarray, n: int, k: int):
    mu = theta[0]
    a_free = theta[1:n]
    b_free = theta[n : 2 * n - 1]
    home_adv = theta[2 * n - 1]
    rho = theta[2 * n]
    w = theta[2 * n + 1 : 2 * n + 1 + k]
    # Sum-to-zero: alpha[n-1] = -sum(alpha_free), same for beta.
    alpha = np.concatenate([a_free, [-a_free.sum()]])
    beta = np.concatenate([b_free, [-b_free.sum()]])
    return mu, alpha, beta, home_adv, rho, w


def neg_log_likelihood(theta: np.ndarray, design: dict, l2: float = 1e-3) -> float:
    """Negative log-likelihood of the DC model with tau correction.

    Uses sum-to-zero identifiability on alpha, beta. L2 penalty applies only
    to the feature weights w (not to alpha/beta, which are mean-zero by
    construction).
    """
    n = design["n_teams"]
    k = design["n_feat"]
    mu, alpha, beta, home_adv, rho, w = _unpack_theta(theta, n, k)

    hi = design["home_idx"]
    ai = design["away_idx"]
    log_lh = mu + alpha[hi] + beta[ai] + home_adv + design["X_home"] @ w
    log_la = mu + alpha[ai] + beta[hi] + design["X_away"] @ w
    # Guard against runaway exponentials during early L-BFGS steps.
    log_lh = np.clip(log_lh, -10.0, 4.0)
    log_la = np.clip(log_la, -10.0, 4.0)
    lam_h = np.exp(log_lh)
    lam_a = np.exp(log_la)

    hg = design["home_goals"]
    ag = design["away_goals"]
    ll = hg * log_lh - lam_h - gammaln(hg + 1)
    ll += ag * log_la - lam_a - gammaln(ag + 1)

    # Vectorised Dixon-Coles tau correction on the 4 low-score cells.
    m00 = (hg == 0) & (ag == 0)
    m01 = (hg == 0) & (ag == 1)
    m10 = (hg == 1) & (ag == 0)
    m11 = (hg == 1) & (ag == 1)
    t = np.ones_like(lam_h)
    t = np.where(m00, 1.0 - lam_h * lam_a * rho, t)
    t = np.where(m01, 1.0 + lam_h * rho, t)
    t = np.where(m10, 1.0 + lam_a * rho, t)
    t = np.where(m11, 1.0 - rho, t)
    # Rho bounds (set in fit()) keep tau > 0 for typical lambda ranges, but
    # clip defensively in case of borderline cells.
    t = np.clip(t, 1e-12, None)
    ll += np.log(t)

    nll = -float(ll.sum()) + 0.5 * l2 * float(w @ w)
    return nll


def fit(
    matches: pd.DataFrame,
    X_home: np.ndarray,
    X_away: np.ndarray,
    *,
    l2: float = 1e-3,
    rho_bounds: tuple[float, float] = (-0.2, 0.2),
    maxiter: int = 500,
) -> DCParams:
    """Fit DCParams on historical matches with L2 regularisation on w.

    Args:
        matches: DataFrame with columns home_id, away_id, home_goals, away_goals.
        X_home: (n_matches, k) feature matrix from the home team's perspective.
        X_away: (n_matches, k) feature matrix from the away team's perspective
            (typically the home vector with direction features sign-flipped and
            confederation one-hots swapped). Pass an (n,0) array for no features.
        l2: ridge penalty on feature weights only.
        rho_bounds: hard bounds on rho. The default (-0.2, 0.2) keeps tau > 0
            for typical lambda < 5; widen only if you know what you're doing.
        maxiter: cap L-BFGS-B iterations.
    """
    design = _build_design(matches, X_home, X_away)
    n = design["n_teams"]
    k = design["n_feat"]
    if n < 2:
        raise ValueError("Need at least 2 distinct teams to fit DC.")
    dim = 1 + (n - 1) + (n - 1) + 1 + 1 + k

    avg_goals = 0.5 * (design["home_goals"].mean() + design["away_goals"].mean())
    theta0 = np.zeros(dim)
    theta0[0] = float(np.log(max(avg_goals, 1e-3)))    # mu
    theta0[2 * n - 1] = 0.3                            # home_adv prior
    theta0[2 * n] = -0.1                               # rho prior (DC original)

    bounds: list[tuple[float | None, float | None]] = [(None, None)] * dim
    bounds[2 * n] = rho_bounds

    result = minimize(
        neg_log_likelihood,
        theta0,
        args=(design, l2),
        method="L-BFGS-B",
        bounds=bounds,
        options={"maxiter": maxiter},
    )
    if not result.success:
        # L-BFGS-B's ABNORMAL_TERMINATION_IN_LNSRCH often fires near a good
        # optimum; surface as a warning so callers can decide.
        warnings.warn(
            f"DC fit did not fully converge: {result.message}", stacklevel=2
        )

    mu, alpha, beta, home_adv, rho, w = _unpack_theta(result.x, n, k)
    teams = design["teams"]
    return DCParams(
        mu=float(mu),
        attack={t: float(alpha[i]) for i, t in enumerate(teams)},
        defence={t: float(beta[i]) for i, t in enumerate(teams)},
        home_adv=float(home_adv),
        rho=float(rho),
        feature_weights=np.asarray(w, dtype=float),
    )


def predict_match(
    params: DCParams,
    home_id: int,
    away_id: int,
    x_home: np.ndarray | None = None,
    x_away: np.ndarray | None = None,
    *,
    neutral: bool = True,
) -> tuple[float, float, float]:
    """Return (P_H, P_D, P_A) for one match from fitted DCParams.

    Set neutral=True for World Cup matches (no home advantage applied) per
    ARCHITECTURE.md §6.1; set False only when the home country is hosting.
    """
    k = len(params.feature_weights)
    xh = np.zeros(k) if x_home is None else np.asarray(x_home, dtype=float)
    xa = np.zeros(k) if x_away is None else np.asarray(x_away, dtype=float)
    if xh.shape != (k,) or xa.shape != (k,):
        raise ValueError(f"feature vectors must have shape ({k},)")
    h = 0.0 if neutral else params.home_adv
    log_lh = params.mu + params.attack[home_id] + params.defence[away_id] + h + xh @ params.feature_weights
    log_la = params.mu + params.attack[away_id] + params.defence[home_id] + xa @ params.feature_weights
    lam_h = float(np.exp(log_lh))
    lam_a = float(np.exp(log_la))
    return match_probs(lam_h, lam_a, params.rho)
