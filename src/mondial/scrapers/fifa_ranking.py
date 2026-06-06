"""FIFA Men's World Ranking — downloads the published CSV from inside.fifa.com.

CSV is regenerated monthly. URL pattern is stable across releases; if it changes,
fall back to scraping the HTML table at https://inside.fifa.com/fifa-world-ranking/men
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

import pandas as pd

from mondial.scrapers.base import RateLimit

CSV_URL = "https://inside.fifa.com/fifa-world-ranking/men.csv"  # update if FIFA changes path


class FifaRankingScraper:
    name = "fifa_ranking"
    rate_limit = RateLimit(requests=1, per_seconds=60)

    def fetch(self, *, since: datetime | None = None) -> list[dict[str, Any]]:
        df = pd.read_csv(CSV_URL)
        return df.to_dict(orient="records")

    def upsert(self, records: list[dict[str, Any]], conn: sqlite3.Connection) -> None:
        # TODO: upsert into teams.fifa_rank, fifa_rank_date
        raise NotImplementedError
