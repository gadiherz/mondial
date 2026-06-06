"""Fetch live WC HDA odds from The Odds API and store them in the DB.

Usage:
    # one-time: get a free key at the-odds-api.com, then in .env:  ODDS_API_KEY=...
    PYTHONPATH=src python scripts/fetch_odds.py

Stores one odds_snapshots row per (match, bookmaker). Safe to re-run; each run
appends a fresh snapshot (the table is a time series). ~12 days before kickoff
only a handful of match markets may be open yet; they fill in nearer the date.
"""
import logging

from mondial.data.db import connect, init_db
from mondial.scrapers.odds_api import OddsAPIScraper


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    init_db()
    scraper = OddsAPIScraper()
    records = scraper.fetch()           # regions='eu' (incl. Pinnacle), market h2h
    logging.getLogger("fetch_odds").info("fetched %d events", len(records))
    with connect() as conn:
        scraper.upsert(records, conn)
        n = conn.execute("SELECT COUNT(*) AS n FROM odds_snapshots").fetchone()["n"]
        n_matches = conn.execute(
            "SELECT COUNT(DISTINCT match_id) AS n FROM odds_snapshots").fetchone()["n"]
    logging.getLogger("fetch_odds").info(
        "odds_snapshots now holds %d rows across %d matches", n, n_matches)


if __name__ == "__main__":
    main()
