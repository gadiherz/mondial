"""Run the pre-kickoff backtest (ARCHITECTURE.md §7.3).

    PYTHONPATH=src python scripts/run_backtest.py                 # all tournaments
    PYTHONPATH=src python scripts/run_backtest.py --tournament euro2024
    PYTHONPATH=src python scripts/run_backtest.py --no-calib      # raw model only (fast)
    PYTHONPATH=src python scripts/run_backtest.py --reliability   # print per-bin tables

Layer 1 (skill/calibration) runs on real results with no odds. Layer 2 (ROI vs
the market) prints a skip notice until historical odds are loaded (open
decision O2). See backtest.py.
"""
from __future__ import annotations

import argparse
import logging

from mondial.eval.backtest import TOURNAMENTS, format_report, run_backtest
from mondial.data.db import connect


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--tournament", choices=list(TOURNAMENTS) + ["all"], default="all")
    ap.add_argument("--no-calib", action="store_true",
                    help="skip Stage-B calibration (raw DC only; faster)")
    ap.add_argument("--calib-method",
                    choices=["temperature_draw", "temperature", "isotonic"],
                    default="temperature_draw",
                    help="Stage-B calibrator (default: temperature_draw, the production choice)")
    ap.add_argument("--reliability", action="store_true",
                    help="also print the pooled per-bin reliability table")
    args = ap.parse_args()

    keys = list(TOURNAMENTS) if args.tournament == "all" else [args.tournament]
    with connect() as conn:
        results = [run_backtest(k, calibrate=not args.no_calib,
                                calib_method=args.calib_method, conn=conn)
                   for k in keys]
    if not args.no_calib:
        print(f"(Stage-B calibrator: {args.calib_method})")

    print()
    print(format_report(results))
    if args.reliability:
        for r in results:
            print(f"\n--- {r.tournament}: pooled reliability (calibrated) ---")
            print(f"{'bin':>10}{'pred':>8}{'emp':>8}{'n':>7}")
            for row in r.reliability:
                print(f"{row['bin']:>10}{row['pred']:>8.3f}{row['emp']:>8.3f}{row['n']:>7}")


if __name__ == "__main__":
    main()
