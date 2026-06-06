"""Scraper contract. See ARCHITECTURE.md §4.3."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol


@dataclass(frozen=True)
class RateLimit:
    requests: int
    per_seconds: int


class Scraper(Protocol):
    name: str
    rate_limit: RateLimit

    def fetch(self, *, since: datetime | None = None) -> list[dict[str, Any]]: ...

    def upsert(self, records: list[dict[str, Any]], conn: sqlite3.Connection) -> None: ...
