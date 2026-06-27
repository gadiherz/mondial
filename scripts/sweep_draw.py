"""Tune the draw-aware decision rule (config.DRAW_THRESHOLD + DRAW_MIN_PROB_EDGE).

The model picks a draw far less often than draws actually occur (~25-30%): the
Purist's plain argmax never selects D, and the Quant's value-pick guard reverts
value-draws to the favorite. The calibration fix makes the draw PROBABILITY honest;
this sweep turns that into an honest PICK by choosing:

  * DRAW_THRESHOLD     -- the Purist (ModelProbs.argmax_draw_aware) picks D when
                          p_draw >= this. Lower => more draws picked.
  * DRAW_MIN_PROB_EDGE -- the Quant (value_pick) draw edge floor. Lower => fewer
                          value-draws reverted to the favorite.

Both are scored on the production-faithful, CALIBRATED probabilities from
eval.backtest.predict_tournament (same DC fit + temperature_draw calibrator + thin-
team filter as production), at FULL resolution. The Purist sweep needs no odds (skill
only); the Quant sweep runs where historical odds exist (WC2018/WC2022).

How to read it: pick DRAW_THRESHOLD so the Purist's pick(D) frequency lands near the
'draw_rate' column ACROSS both WCs (the goal: pick draws ~as often as they happen),
preferring a value that does not crater accuracy. Pick DRAW_MIN_PROB_EDGE similarly
for the Quant, watching ROI -- the draw floor should lift pick(D) toward the base
rate without turning ROI sharply negative. Then set both in src/mondial/config.py.

Usage: PYTHONPATH=src python scripts/sweep_draw.py
"""
from __future__ import annotations

import numpy as np

from mondial.config import MIN_PROB_EDGE
from mondial.data.db import connect
from mondial.eval import simulator as sim
from mondial.eval.backtest import TOURNAMENTS, _load_odds, predict_tournament
from mondial.eval.metrics import roi

THRESH_GRID = [0.26, 0.28, 0.30, 0.32, 0.34, 0.36]
DRAW_EDGE_GRID = [0.00, 0.01, 0.02, 0.03, 0.05, 0.08]


def _purist(probs, outs, thr):
    """Purist (argmax_draw_aware) pick(D) frequency + accuracy at one threshold."""
    picks = [sim.ModelProbs(*probs[i].tolist()).argmax_draw_aware(thr)
             for i in range(len(outs))]
    actual = [("H", "D", "A")[int(o)] for o in outs]
    n = len(outs)
    draw_freq = sum(p == "D" for p in picks) / n if n else float("nan")
    acc = sum(p == a for p, a in zip(picks, actual)) / n if n else float("nan")
    return draw_freq, acc


def _quant(probs, outs, kept_ids, odds, draw_edge):
    """Quant (value_pick) pick(D) freq, accuracy + ROI at one draw edge floor."""
    stake = payout = 0.0
    picks, actuals = [], []
    for i, mid in enumerate(kept_ids):
        o = odds.get(mid)
        if o is None:
            continue
        mp = sim.ModelProbs(*probs[i].tolist())
        pick = sim.value_pick(mp, o, MIN_PROB_EDGE, draw_edge)
        actual = ("H", "D", "A")[int(outs[i])]
        picks.append(pick)
        actuals.append(actual)
        stake += sim.STAKE_NIS
        payout += sim.settle(pick, actual, o.by_outcome(pick))
    n = len(picks)
    draw_freq = sum(p == "D" for p in picks) / n if n else float("nan")
    acc = sum(p == a for p, a in zip(picks, actuals)) / n if n else float("nan")
    r = roi(np.array([stake]), np.array([payout])) if n else float("nan")
    return draw_freq, acc, r, n


def main() -> None:
    with connect() as conn:
        prepared = []
        for key in TOURNAMENTS:
            pred = predict_tournament(conn, key, calibrate=True)
            odds = _load_odds(conn, pred.kept_ids)
            prepared.append((pred.label, pred.cal_probs, pred.outs, pred.kept_ids, odds))

    print("\n=== Purist: DRAW_THRESHOLD sweep (pick(D) freq vs real draw rate; "
          "no odds needed) ===")
    for label, probs, outs, _ids, _odds in prepared:
        draw_rate = float(np.mean(outs == 1))
        print(f"\n  {label}  (n={len(outs)}, real draw_rate={draw_rate:.3f})")
        print(f"  {'thr':>6}{'pick(D)%':>10}{'acc':>8}")
        for thr in THRESH_GRID:
            f, acc = _purist(probs, outs, thr)
            print(f"  {thr:6.2f}{f:10.1%}{acc:8.3f}")

    print("\n=== Quant: DRAW_MIN_PROB_EDGE sweep (needs odds; "
          f"underdog floor fixed at MIN_PROB_EDGE={MIN_PROB_EDGE:g}) ===")
    for label, probs, outs, kept_ids, odds in prepared:
        if not odds:
            print(f"\n  {label}: no stored odds -- skipped (see O2).")
            continue
        draw_rate = float(np.mean(outs == 1))
        print(f"\n  {label}  (real draw_rate={draw_rate:.3f})")
        print(f"  {'edge':>6}{'pick(D)%':>10}{'acc':>8}{'ROI':>9}{'n':>5}")
        for e in DRAW_EDGE_GRID:
            f, acc, r, n = _quant(probs, outs, kept_ids, odds, e)
            print(f"  {e:6.2f}{f:10.1%}{acc:8.3f}{r:9.1%}{n:5d}")

    print("\nChoose DRAW_THRESHOLD so the Purist pick(D)% lands near draw_rate across")
    print("both WCs; choose DRAW_MIN_PROB_EDGE likewise for the Quant without cratering")
    print("ROI. Set both in src/mondial/config.py.")


if __name__ == "__main__":
    main()
