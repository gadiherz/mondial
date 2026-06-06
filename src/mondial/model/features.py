"""Feature engineering for a (home, away) match. See ARCHITECTURE.md §5.

V1 feature set is the Glicko-2 + momentum ("black-swan") signals produced by the
leakage-free streaming backfill in `mondial.model.ratings`:

  * for TRAINING, the per-match PRE-match state lives in the `match_features`
    table (one row per final match, written before that match's rating update);
  * for PREDICTION of scheduled matches, the latest per-team snapshot lives in
    `team_state`.

Both expose the same ten raw quantities (home/away each of: Glicko rating, RD,
residual-EWMA, goal-diff-EWMA, idle-days), so a single vector builder serves
both paths.

How features enter the Dixon-Coles goal model (see dixon_coles.py): the SAME
weight vector w multiplies both the home-perspective vector x_home (added to
log lambda_home) and the away-perspective vector x_away (added to
log lambda_away). Therefore:

  * a DIRECTIONAL signal (e.g. "home is rated higher") must be ANTISYMMETRIC:
    x_away = -x_home. A positive weight then raises the stronger side's rate and
    lowers the weaker side's, as intended.
  * a SHARED signal (e.g. "total rating uncertainty in this match") is SYMMETRIC:
    x_away = x_home, shifting both goal rates together.

`SIGN` encodes this per feature; the away vector is always `x_home * SIGN`.

Deferred to V1.1 (documented, not wired): the rating-deviation (RD) fat-tail
mechanism in its theoretically-correct form -- integrating the outcome
distribution over the Glicko rating posterior to give underdogs extra
probability mass (glicko.py's docstring). That requires widening the score grid
in dixon_coles.match_probs, not a linear log-lambda term. V1 instead exposes RD
as the symmetric `rd_sum` feature so the fit can use total match uncertainty;
true posterior integration is a follow-up.

Also deferred (data not yet scraped / not in match_features): FIFA-rank diff,
squad-value, coach, confederation one-hots, and group-vs-knockout stage.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

from mondial.config import settings
from mondial.model import glicko

# --- Feature scaling -------------------------------------------------------
# Raw quantities are scaled to roughly unit range so the L2 penalty on the
# feature weights is comparable across features and L-BFGS-B is well-conditioned.
GLICKO_SCALE = 400.0          # Glicko rating diff; ~Elo points, typical |diff| < 600
RD_SCALE = glicko.DEFAULT_RD  # 350.0; sum of two RDs spans ~[60, 700]
GD_SCALE = 4.0                # goal-diff EWMA is a decayed mean of gd capped at +-4
IDLE_SCALE = 365.0            # idle days -> ~[0, 1+] for up to a year of inactivity


@dataclass(frozen=True)
class FeatureSpec:
    name: str
    antisymmetric: bool   # True: x_away = -x_home (directional); False: shared
    description: str


SPEC: tuple[FeatureSpec, ...] = (
    FeatureSpec("glicko_diff", True,
                "Glicko-2 rating, (home - away) / 400 (positive = home stronger)"),
    FeatureSpec("rd_sum", False,
                "Total pre-match rating uncertainty, (home_rd + away_rd) / 350"),
    FeatureSpec("resid_ewma_diff", True,
                "Momentum: EWMA result-minus-Glicko-expectation, home - away "
                "(positive = home over-performing lately / underrated)"),
    FeatureSpec("gd_ewma_diff", True,
                "Recent goal-difference form, (home - away) / 4"),
    FeatureSpec("idle_days_diff", True,
                "Rest/rust: (home_idle_days - away_idle_days) / 365"),
)

# Multiply a home-perspective vector by SIGN to obtain the away-perspective one.
SIGN: np.ndarray = np.array(
    [-1.0 if s.antisymmetric else 1.0 for s in SPEC], dtype=float
)

# Raw match_features / team_state column groups, in the order _home_vector reads.
_RAW_HOME = ("home_glicko", "home_rd", "home_resid_ewma", "home_gd_ewma", "home_idle_days")
_RAW_AWAY = ("away_glicko", "away_rd", "away_resid_ewma", "away_gd_ewma", "away_idle_days")


def n_features() -> int:
    return len(SPEC)


def feature_names() -> list[str]:
    return [s.name for s in SPEC]


def _home_vector(
    home_glicko: float, away_glicko: float,
    home_rd: float, away_rd: float,
    home_resid: float, away_resid: float,
    home_gd: float, away_gd: float,
    home_idle: float, away_idle: float,
) -> np.ndarray:
    """Assemble the home-perspective feature vector in SPEC order."""
    return np.array([
        (home_glicko - away_glicko) / GLICKO_SCALE,   # glicko_diff
        (home_rd + away_rd) / RD_SCALE,               # rd_sum (symmetric)
        (home_resid - away_resid),                    # resid_ewma_diff
        (home_gd - away_gd) / GD_SCALE,               # gd_ewma_diff
        (home_idle - away_idle) / IDLE_SCALE,         # idle_days_diff
    ], dtype=float)


def design_matrices(feat: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    """Build (X_home, X_away) for TRAINING from rows joined to `match_features`.

    `feat` must contain the ten raw columns (home_/away_ x glicko, rd,
    resid_ewma, gd_ewma, idle_days). Rows are processed in the given order, so
    `feat` must be aligned 1:1 with the `matches` rows passed to dixon_coles.fit.
    """
    missing = [c for c in (*_RAW_HOME, *_RAW_AWAY) if c not in feat.columns]
    if missing:
        raise ValueError(f"match_features columns missing from frame: {missing}")

    hg = feat["home_glicko"].to_numpy(float)
    ag = feat["away_glicko"].to_numpy(float)
    hr = feat["home_rd"].to_numpy(float)
    ar = feat["away_rd"].to_numpy(float)
    hres = feat["home_resid_ewma"].to_numpy(float)
    ares = feat["away_resid_ewma"].to_numpy(float)
    hgd = feat["home_gd_ewma"].to_numpy(float)
    agd = feat["away_gd_ewma"].to_numpy(float)
    hi = feat["home_idle_days"].to_numpy(float)
    ai = feat["away_idle_days"].to_numpy(float)

    X_home = np.column_stack([
        (hg - ag) / GLICKO_SCALE,
        (hr + ar) / RD_SCALE,
        (hres - ares),
        (hgd - agd) / GD_SCALE,
        (hi - ai) / IDLE_SCALE,
    ])
    X_away = X_home * SIGN
    return X_home, X_away


def _idle_days(match_date: str, last_match_date: str | None) -> float:
    if not last_match_date:
        return 0.0
    return max((date.fromisoformat(match_date) - date.fromisoformat(last_match_date)).days, 0.0)


def _intel_glicko_delta(conn: sqlite3.Connection, team_id: int) -> float:
    """LLM-intel nudge to a team's effective Glicko rating, in rating points.

    Reads `team_intel.composite` (in [-1, 1], written by the autonomous logger)
    and scales it by `settings.intel_glicko_points` ("full power" magnitude).
    Returns 0.0 when no intel exists for the team (neutral default) -- the
    prediction pipeline reports overall intel coverage, so a missing row is
    visible there rather than logged per-match here. PREDICTION-ONLY: this never
    touches the training path (match_features has no intel column); it rides the
    already-fitted glicko_diff weight at predict time.
    """
    row = conn.execute(
        "SELECT composite FROM team_intel WHERE team_id=?", (team_id,)
    ).fetchone()
    if row is None or row["composite"] is None:
        return 0.0
    return float(row["composite"]) * settings.intel_glicko_points


def build_feature_vector(
    home_id: int, away_id: int, match_date: str, conn: sqlite3.Connection,
) -> tuple[np.ndarray, np.ndarray]:
    """Build (x_home, x_away) for PREDICTING one scheduled match.

    Reads each team's latest snapshot from `team_state` (populated by
    mondial.model.ratings.stream_backfill). Idle days are measured from each
    team's last match up to `match_date`.

    Raises LookupError -- loudly, never a silent zero-feature fallback -- if
    either team has no team_state row (e.g. backfill not run, or a brand-new
    side). Callers decide how to handle (skip the fixture, etc.).
    """
    rows = {
        r["team_id"]: r
        for r in conn.execute(
            """SELECT team_id, glicko, rd, resid_ewma, gd_ewma, last_match_date
               FROM team_state WHERE team_id IN (?, ?)""",
            (home_id, away_id),
        ).fetchall()
    }
    missing = [tid for tid in (home_id, away_id) if tid not in rows]
    if missing:
        raise LookupError(
            f"team_state missing for team_id(s) {missing}; run "
            f"`python -m mondial.pipelines.train --bootstrap` to populate."
        )

    h, a = rows[home_id], rows[away_id]
    # Track B: nudge each team's effective rating by its LLM-intel composite,
    # so it flows through the fitted glicko_diff weight.
    home_glicko = h["glicko"] + _intel_glicko_delta(conn, home_id)
    away_glicko = a["glicko"] + _intel_glicko_delta(conn, away_id)
    x_home = _home_vector(
        home_glicko, away_glicko,
        h["rd"], a["rd"],
        h["resid_ewma"], a["resid_ewma"],
        h["gd_ewma"], a["gd_ewma"],
        _idle_days(match_date, h["last_match_date"]),
        _idle_days(match_date, a["last_match_date"]),
    )
    return x_home, x_home * SIGN
