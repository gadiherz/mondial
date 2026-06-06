"""football-data.org scraper. Free tier: 10 req/min.

Docs: https://www.football-data.org/documentation/quickstart
Competitions of interest: WC=2000, EC=2018, CL=2001, etc.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

import requests
from tenacity import retry, stop_after_attempt, wait_exponential

from mondial.config import settings
from mondial.scrapers.base import RateLimit

BASE = "https://api.football-data.org/v4"


class FootballDataScraper:
    name = "football_data"
    rate_limit = RateLimit(requests=10, per_seconds=60)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=30))
    def fetch(self, *, since: datetime | None = None) -> list[dict[str, Any]]:
        if not settings.football_data_token:
            raise RuntimeError("FOOTBALL_DATA_TOKEN not set")
        url = f"{BASE}/competitions/WC/matches"
        headers = {"X-Auth-Token": settings.football_data_token}
        params: dict[str, Any] = {}
        if since:
            params["dateFrom"] = since.date().isoformat()
        resp = requests.get(url, headers=headers, params=params, timeout=30)
        resp.raise_for_status()
        return resp.json().get("matches", [])

    def upsert(self, records: list[dict[str, Any]], conn: sqlite3.Connection) -> None:
        # TODO: map records into teams + matches tables
        raise NotImplementedError
