"""Fetch Winner (Israeli TOTO) 1X2 odds via the bankerim aggregator and store them.

Winner is the only legal Israeli book, so its line is the WINNER-16 eval reference
(ARCHITECTURE D2). Source is the bankerim.co.il mirror (no bot wall, CI-safe);
see scrapers/winner_odds.py for why not the Incapsula-walled official API.

Usage:
    PYTHONPATH=src python scripts/fetch_winner_odds.py            # coupon + full line
    PYTHONPATH=src python scripts/fetch_winner_odds.py --no-line  # coupon only
"""
import argparse
import logging

from mondial.data.db import connect, init_db
from mondial.scrapers.winner_odds import WinnerOddsScraper


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-line", action="store_true",
                    help="parse only the Winner-16 coupon, not the full line")
    args = ap.parse_args()

    init_db()
    scraper = WinnerOddsScraper()
    records = scraper.fetch(line=not args.no_line)
    with connect() as conn:
        scraper.upsert(records, conn)
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM odds_snapshots WHERE bookmaker='winner'"
        ).fetchone()["n"]
    logging.getLogger("fetch_winner_odds").info(
        "odds_snapshots now holds %d 'winner' rows", n)


if __name__ == "__main__":
    main()
