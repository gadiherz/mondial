"""Evaluation metrics. See ARCHITECTURE.md §7.2."""
from __future__ import annotations

import numpy as np

EPS = 1e-12


def brier_score(probs: np.ndarray, outcomes: np.ndarray) -> float:
    """probs: (n,3) ; outcomes: (n,) ints in {0,1,2}. Lower is better."""
    onehot = np.zeros_like(probs)
    onehot[np.arange(len(outcomes)), outcomes] = 1.0
    return float(np.mean(np.sum((probs - onehot) ** 2, axis=1)))


def log_loss(probs: np.ndarray, outcomes: np.ndarray) -> float:
    p = np.clip(probs[np.arange(len(outcomes)), outcomes], EPS, 1.0)
    return float(-np.mean(np.log(p)))


def roi(stakes: np.ndarray, payouts: np.ndarray) -> float:
    total_stake = float(stakes.sum())
    if total_stake == 0:
        return 0.0
    return float((payouts.sum() - total_stake) / total_stake)


def reliability_bins(probs_pos: np.ndarray, outcomes_pos: np.ndarray, n_bins: int = 10):
    """Return (bin_centers, predicted_mean, empirical_mean, counts) for a single class."""
    edges = np.linspace(0, 1, n_bins + 1)
    idx = np.clip(np.digitize(probs_pos, edges) - 1, 0, n_bins - 1)
    pred = np.zeros(n_bins)
    emp = np.zeros(n_bins)
    cnt = np.zeros(n_bins, dtype=int)
    for b in range(n_bins):
        mask = idx == b
        cnt[b] = int(mask.sum())
        if cnt[b]:
            pred[b] = probs_pos[mask].mean()
            emp[b] = outcomes_pos[mask].mean()
    centers = 0.5 * (edges[:-1] + edges[1:])
    return centers, pred, emp, cnt
