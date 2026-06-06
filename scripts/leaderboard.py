"""Six-player betting leaderboard (the live sim's standings).

    PYTHONPATH=src python scripts/leaderboard.py            # show standings
    PYTHONPATH=src python scripts/leaderboard.py --place    # place bets first
    PYTHONPATH=src python scripts/leaderboard.py --settle   # settle finals first

Bets are placed at the Winner line for priced+predicted scheduled matches and
settled once results are final. See eval/betting.py. In normal operation Routine A
places and Routine B settles; the flags are for manual/local runs.
"""
import argparse
import logging

from mondial.data.db import connect, init_db
from mondial.eval.betting import leaderboard, place_bets, settle_bets


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--place", action="store_true", help="place bets before showing")
    ap.add_argument("--settle", action="store_true", help="settle finals before showing")
    args = ap.parse_args()

    init_db()
    with connect() as conn:
        if args.place:
            place_bets(conn)
        if args.settle:
            settle_bets(conn)
        board = leaderboard(conn)

    if not board:
        print("No bets yet. Place some: `python scripts/leaderboard.py --place` "
              "(needs predictions + Winner odds in the DB).")
        return
    print(f"\n{'player':<20}{'bets':>6}{'settled':>9}{'staked':>9}"
          f"{'returned':>10}{'profit':>9}{'ROI':>8}")
    print("-" * 71)
    for r in board:
        roi = f"{r['roi']:+.1%}" if r["roi"] is not None else "--"
        print(f"{r['player']:<20}{r['n_bets']:>6}{r['n_settled']:>9}{r['staked']:>9.0f}"
              f"{r['returned']:>10.1f}{r['profit']:>+9.1f}{roi:>8}")
    print("\nROI is on SETTLED bets only; 'staked' is total exposure (stake=10 NIS/bet).")


if __name__ == "__main__":
    main()
