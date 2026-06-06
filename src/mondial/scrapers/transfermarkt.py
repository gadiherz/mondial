"""Transfermarkt squad market values.

No official API; use the community project `transfermarkt-api` (FastAPI
self-hosted) or `pytransfermarkt`. Both wrap the public site.

V1: implement against transfermarkt-api running locally or against a public
instance such as https://transfermarkt-api.fly.dev/.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

import requests
from tenacity import retry, stop_after_attempt, wait_exponential

from mondial.scrapers.base import RateLimit

BASE = "https://transfermarkt-api.fly.dev"


class TransfermarktScraper:
    name = "transfermarkt"
    rate_limit = RateLimit(requests=30, per_seconds=60)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=30))
    def fetch(self, *, since: datetime | None = None) -> list[dict[str, Any]]:
        # TODO: iterate over national-team IDs for 48 WC 2026 participants
        raise NotImplementedError

    def upsert(self, records: list[dict[str, Any]], conn: sqlite3.Connection) -> None:
        raise NotImplementedError
