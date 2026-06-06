"""Back-fill historical 1X2 odds into odds_snapshots (backtest ROI layer, O2).

    # football-data.co.uk World Cup sheets (free, no key) -- default WC 2022:
    PYTHONPATH=src python scripts/ingest_historical_odds.py
    PYTHONPATH=src python scripts/ingest_historical_odds.py --fd-sheet WorldCup2018

    # a generic per-match CSV you dropped in data/historical_odds/ (e.g. Kaggle
    # Euro 2024). Give the column names; odds tag -> H,D,A columns:
    PYTHONPATH=src python scripts/ingest_historical_odds.py \
        --csv data/historical_odds/euro2024.csv \
        --date Date --home HomeTeam --away AwayTeam \
        --odds b365:B365H,B365D,B365A --odds avg:AvgH,AvgD,AvgA

See src/mondial/scrapers/historical_odds.py. Euro 2024 is NOT on football-data
(no Euro file); use the --csv path with a Kaggle export for it.
"""
from __future__ import annotations

import argparse
import logging

from mondial.data.db import connect, init_db
from mondial.scrapers.historical_odds import ingest_csv, ingest_fd_worldcup


def _parse_odds(specs: list[str]) -> dict[str, tuple[str, str, str]]:
    out: dict[str, tuple[str, str, str]] = {}
    for s in specs or []:
        tag, cols = s.split(":", 1)
        h, d, a = (c.strip() for c in cols.split(","))
        out[tag.strip()] = (h, d, a)
    return out


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--fd-sheet", default="WorldCup2022",
                    help="football-data workbook sheet (WorldCup2022/2018/2014)")
    ap.add_argument("--csv", help="generic per-match 1X2 CSV instead of the FD sheet")
    ap.add_argument("--date", help="date column (CSV mode)")
    ap.add_argument("--home", help="home-team column (CSV mode)")
    ap.add_argument("--away", help="away-team column (CSV mode)")
    ap.add_argument("--odds", action="append",
                    help="CSV mode, repeatable: tag:Hcol,Dcol,Acol")
    args = ap.parse_args()

    init_db()
    with connect() as conn:
        if args.csv:
            if not all((args.date, args.home, args.away, args.odds)):
                ap.error("--csv requires --date, --home, --away and at least one --odds")
            ingest_csv(conn, args.csv, date_col=args.date, home_col=args.home,
                       away_col=args.away, odds_cols=_parse_odds(args.odds))
        else:
            ingest_fd_worldcup(conn, sheet=args.fd_sheet)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
