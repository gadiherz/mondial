import math

import numpy as np
import pandas as pd
import pytest

from mondial.model.dixon_coles import (
    _build_design,
    _unpack_theta,
    fit,
    match_probs,
    neg_log_likelihood,
    predict_match,
    tau,
)


def test_match_probs_sum_to_one():
    p_h, p_d, p_a = match_probs(lam_home=1.5, lam_away=1.2, rho=-0.1)
    assert math.isclose(p_h + p_d + p_a, 1.0, rel_tol=1e-6)


def test_tau_only_corrects_low_scores():
    assert tau(3, 2, 1.5, 1.2, -0.1) == 1.0
    assert tau(0, 0, 1.5, 1.2, -0.1) != 1.0


def test_match_probs_home_stronger_more_likely():
    p_h, _, p_a = match_probs(lam_home=2.0, lam_away=0.8, rho=0.0)
    assert p_h > p_a


def _toy_matches():
    return pd.DataFrame({
        "home_id": [1, 2, 3, 1, 2, 3],
        "away_id": [2, 3, 1, 3, 1, 2],
        "home_goals": [1, 2, 0, 3, 1, 1],
        "away_goals": [1, 0, 1, 1, 2, 1],
    })


def test_build_design_rejects_missing_columns():
    bad = pd.DataFrame({"home_id": [1], "away_id": [2], "home_goals": [1]})
    with pytest.raises(ValueError, match="missing required columns"):
        _build_design(bad, np.zeros((1, 0)), np.zeros((1, 0)))


def test_build_design_rejects_row_mismatch():
    matches = _toy_matches()
    with pytest.raises(ValueError, match="row count"):
        _build_design(matches, np.zeros((3, 2)), np.zeros((3, 2)))


def test_neg_log_likelihood_finite_at_zero():
    matches = _toy_matches()
    X = np.zeros((len(matches), 2))
    design = _build_design(matches, X, X)
    n, k = design["n_teams"], design["n_feat"]
    dim = 1 + (n - 1) + (n - 1) + 1 + 1 + k
    theta = np.zeros(dim)
    nll = neg_log_likelihood(theta, design, l2=0.0)
    assert math.isfinite(nll)
    assert nll > 0


def test_unpack_theta_sum_to_zero():
    n, k = 4, 2
    dim = 1 + (n - 1) + (n - 1) + 1 + 1 + k
    rng = np.random.default_rng(0)
    theta = rng.normal(size=dim)
    _, alpha, beta, _, _, _ = _unpack_theta(theta, n, k)
    assert math.isclose(alpha.sum(), 0.0, abs_tol=1e-12)
    assert math.isclose(beta.sum(), 0.0, abs_tol=1e-12)
    assert alpha.shape == (n,)
    assert beta.shape == (n,)


def test_fit_recovers_attack_ranking():
    """Team 1 has the highest true attack; the fit should recover this ordering."""
    rng = np.random.default_rng(0)
    teams = [1, 2, 3]
    true_attack = {1: 0.8, 2: -0.2, 3: -0.6}
    true_defence = {1: -0.3, 2: 0.1, 3: 0.2}
    mu_true, home_true = 0.4, 0.25

    rows = []
    for _ in range(500):
        h, a = (int(x) for x in rng.choice(teams, size=2, replace=False))
        lam_h = math.exp(mu_true + true_attack[h] + true_defence[a] + home_true)
        lam_a = math.exp(mu_true + true_attack[a] + true_defence[h])
        rows.append((h, a, int(rng.poisson(lam_h)), int(rng.poisson(lam_a))))
    matches = pd.DataFrame(rows, columns=["home_id", "away_id", "home_goals", "away_goals"])
    X = np.zeros((len(matches), 0))
    params = fit(matches, X, X, l2=0.0)

    assert params.attack[1] > params.attack[2]
    assert params.attack[1] > params.attack[3]
    # Higher beta = weaker defence, so team 3 should have the highest beta.
    assert params.defence[3] > params.defence[1]
    assert params.home_adv > 0
    # Sum-to-zero identifiability holds on the returned params.
    assert math.isclose(sum(params.attack.values()), 0.0, abs_tol=1e-8)
    assert math.isclose(sum(params.defence.values()), 0.0, abs_tol=1e-8)


def test_fit_recovers_feature_weight_sign():
    """A single feature that boosts home goals should fit a positive weight."""
    rng = np.random.default_rng(1)
    teams = [1, 2, 3, 4]
    matches_rows = []
    x_rows = []
    for _ in range(400):
        h, a = (int(x) for x in rng.choice(teams, size=2, replace=False))
        x = float(a - h) * 0.4
        lam_h = math.exp(0.4 + 0.5 * x + 0.2)
        lam_a = math.exp(0.4 - 0.5 * x)
        matches_rows.append((h, a, int(rng.poisson(lam_h)), int(rng.poisson(lam_a))))
        x_rows.append([x])
    matches = pd.DataFrame(matches_rows, columns=["home_id", "away_id", "home_goals", "away_goals"])
    X_home = np.array(x_rows)
    X_away = -X_home
    params = fit(matches, X_home, X_away, l2=1e-4)
    assert params.feature_weights[0] > 0


def test_predict_match_neutral_vs_home():
    """Same teams should be more home-favoured when neutral=False."""
    params_minimal = fit(_toy_matches(), np.zeros((6, 0)), np.zeros((6, 0)), l2=0.0)
    p_neutral = predict_match(params_minimal, 1, 2, neutral=True)
    p_home = predict_match(params_minimal, 1, 2, neutral=False)
    assert p_home[0] > p_neutral[0]
    for p in (p_neutral, p_home):
        assert math.isclose(sum(p), 1.0, rel_tol=1e-6)


def test_predict_match_uses_features():
    matches = _toy_matches()
    X = np.zeros((len(matches), 1))
    params = fit(matches, X, X, l2=0.0)
    # Force a known feature weight so the test is deterministic.
    params.feature_weights[:] = 0.5
    p_no = predict_match(params, 1, 2, x_home=np.array([0.0]), x_away=np.array([0.0]))
    p_pos = predict_match(params, 1, 2, x_home=np.array([1.0]), x_away=np.array([-1.0]))
    assert p_pos[0] > p_no[0]  # positive home feature -> more home wins
