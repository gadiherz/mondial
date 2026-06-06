"""Calibrate the value-picker min-edge guard (`min_prob_edge`) on real odds.

For each tournament that has stored historical odds, this sweeps tau over a grid
and re-scores the MODEL player's picks at each tau -- reporting ROI, how many
picks were contrarian (differ from the market favorite = lowest odd), and whether
those contrarian picks beat their own breakeven hit-rate (the implied 1/odd they
must clear to profit). The expensive stage (DC fit + per-match prediction) runs
ONCE per tournament via eval.backtest.predict_tournament and is reused across all
taus; only the cheap pick/settle loop varies -- so every tau is evaluated at full
resolution, never on a downsampled slice.

How to read it: pick tau on a STABLE, POSITIVE plateau that holds across more than
one tournament AND where the contrarian picks beat breakeven (edge=yes) -- NOT the
single highest ROI. WC 2022 is 64 matches; a lone ROI peak there is almost
certainly upset variance, not edge (the dumb `risk`/longshot player out-earns the
model on WC22). tau=0.00 reproduces the old unguarded behaviour as the baseline.

Usage: PYTHONPATH=src python scripts/sweep_min_edge.py
       PYTHONPATH=src python scripts/sweep_min_edge.py --raw   # gate on raw (uncalibrated) probs
"""
from __future__ import annotations

import argparse

import numpy as np

from mondial.data.db import connect
from mondial.eval import simulator as sim
from mondial.eval.backtest import TOURNAMENTS, _load_odds, predict_tournament
from mondial.eval.metrics import roi

GRID = [0.00, 0.02, 0.03, 0.04, 0.05, 0.06, 0.08, 0.10, 0.12, 0.15]


def _score(probs, outs, kept_ids, odds, tau):
    """ROI + contrarian stats for the model player at one tau."""
    stake = payout = 0.0
    n_contra = contra_hits = 0
    breakeven_sum = 0.0  # sum of 1/odd over contrarian picks -> mean breakeven hit-rate
    for i, mid in enumerate(kept_ids):
        o = odds.get(mid)
        if o is None:
            continue
        mp = sim.ModelProbs(*probs[i].tolist())
        pick = sim.value_pick(mp, o, tau)
        actual = ("H", "D", "A")[int(outs[i])]
        odd = o.by_outcome(pick)
        stake += sim.STAKE_NIS
        payout += sim.settle(pick, actual, odd)
        if pick != o.lowest():  # contrarian: deserted the market favorite
            n_contra += 1
            breakeven_sum += 1.0 / odd
            if pick == actual:
                contra_hits += 1
    r = roi(np.array([stake]), np.array([payout]))
    rate = contra_hits / n_contra if n_contra else float("nan")
    breakeven = breakeven_sum / n_contra if n_contra else float("nan")
    return r, n_contra, rate, breakeven


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", action="store_true",
                    help="gate on raw (uncalibrated) probs -- WC22's isotonic "
                         "calibrator is over-confident on upsets, so raw may be fairer")
    args = ap.parse_args()
    calibrate = not args.raw

    with connect() as conn:
        prepared = []
        for key in TOURNAMENTS:
            pred = predict_tournament(conn, key, calibrate=calibrate)
            odds = _load_odds(conn, pred.kept_ids)
            probs = pred.raw_probs if args.raw else pred.cal_probs
            prepared.append((pred.label, probs, pred.outs, pred.kept_ids, odds))

    print(f"\nmin_prob_edge sweep ({'RAW' if args.raw else 'CALIBRATED'} probs); "
          "'edge?'=do contrarian picks beat their breakeven hit-rate")
    for label, probs, outs, kept_ids, odds in prepared:
        print(f"\n=== {label} ===")
        if not odds:
            print("  no stored odds -- ROI layer skipped (load historical odds first; see O2).")
            continue
        print(f"  {'tau':>5}{'ROI':>9}{'#contra':>9}{'hit%':>8}{'breakeven%':>12}{'edge?':>7}")
        for tau in GRID:
            r, nc, rate, be = _score(probs, outs, kept_ids, odds, tau)
            if np.isnan(rate):
                rate_s, be_s, edge = "--", "--", "--"
            else:
                rate_s, be_s = f"{rate:.1%}", f"{be:.1%}"
                edge = "yes" if rate > be else "no"
            print(f"  {tau:5.2f}{r:8.1%}{nc:9d}{rate_s:>8}{be_s:>12}{edge:>7}")
    print("\nChoose tau on a stable, positive plateau across tournaments where the")
    print("contrarian picks beat breakeven (edge=yes) -- not the single best ROI.")
    print("Then set MIN_PROB_EDGE in src/mondial/config.py to that value.")


if __name__ == "__main__":
    main()
