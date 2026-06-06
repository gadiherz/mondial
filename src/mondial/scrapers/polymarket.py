"""Polymarket public API (CLOB + Gamma).

Docs: https://docs.polymarket.com/
Gamma is best for discovering markets; CLOB for order-book data.

V1: query Gamma for WC 2026 match markets, take the YES price as P(home win),
NO as P(away win or draw). For 3-way markets, take three legs.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

import requests
from tenacity import retry, stop_after_attempt, wait_exponential

from mondial.scrapers.base import RateLimit

GAMMA = "https://gamma-api.polymarket.com"


class PolymarketScraper:
    name = "polymarket"
    rate_limit = RateLimit(requests=60, per_seconds=60)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=30))
    def fetch(self, *, since: datetime | None = None) -> list[dict[str, Any]]:
        resp = requests.get(
            f"{GAMMA}/markets",
            params={"closed": "false", "tag": "fifa-world-cup"},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()

    def upsert(self, records: list[dict[str, Any]], conn: sqlite3.Connection) -> None:
        # TODO: insert into odds_snapshots with bookmaker='polymarket'
        raise NotImplementedError
