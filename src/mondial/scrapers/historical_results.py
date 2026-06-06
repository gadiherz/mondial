"""Historical international match results bootstrap.

Source: Mart Jurisoo's "International football results from 1872 to present"
(github.com/martj42/international_results), refreshed regularly. Free, no API
key, ~50k rows, ~12k since 2014-01-01.

This is the one-shot training-data loader (ARCHITECTURE.md §6.3). The CSV
already includes scheduled-but-unplayed fixtures (NaN scores) -- we keep
those as `status='scheduled'` so they're available as prediction targets.
"""
from __future__ import annotations

import math
import sqlite3
from datetime import datetime
from io import StringIO
from typing import Any

import pandas as pd
import requests
from tenacity import retry, stop_after_attempt, wait_exponential

from mondial.scrapers.base import RateLimit

CSV_URL = "https://raw.githubusercontent.com/martj42/international_results/master/results.csv"
TRAINING_START = "2014-01-01"

# Map the dataset's tournament strings to the K-factor categories that
# `mondial.model.elo.k_factor` understands. Anything not listed falls back
# to FRIENDLY (K=30), which is the appropriate default for minor / regional
# tournaments where we want low Elo movement.
TOURNAMENT_MAP: dict[str, str] = {
    "FIFA World Cup": "WC",
    "FIFA World Cup qualification": "WC_QUAL",
    "UEFA Euro": "EURO",
    "UEFA Euro qualification": "WC_QUAL",
    "Copa América": "COPA",
    "African Cup of Nations": "AFCON",
    "African Cup of Nations qualification": "WC_QUAL",
    "AFC Asian Cup": "ASIAN_CUP",
    "AFC Asian Cup qualification": "WC_QUAL",
    "UEFA Nations League": "NATIONS",
    "CONCACAF Nations League": "NATIONS",
    "CONCACAF Nations League qualification": "NATIONS",
    "Confederations Cup": "WC",
    "Gold Cup": "AFCON",
    "Friendly": "FRIENDLY",
}

# Minimal hand-curated confederation map for the teams that dominate the
# training set and the WC 2026 field. Anything else is 'UNKNOWN' and can be
# backfilled later by the FIFA ranking scraper (which carries confederation
# in its CSV). Schema requires NOT NULL so we always write something.
CONFEDERATIONS: dict[str, str] = {
    # UEFA
    "Germany": "UEFA", "France": "UEFA", "England": "UEFA", "Spain": "UEFA",
    "Italy": "UEFA", "Portugal": "UEFA", "Netherlands": "UEFA", "Belgium": "UEFA",
    "Croatia": "UEFA", "Switzerland": "UEFA", "Denmark": "UEFA", "Poland": "UEFA",
    "Sweden": "UEFA", "Austria": "UEFA", "Ukraine": "UEFA", "Norway": "UEFA",
    "Wales": "UEFA", "Scotland": "UEFA", "Czech Republic": "UEFA", "Serbia": "UEFA",
    "Hungary": "UEFA", "Turkey": "UEFA", "Romania": "UEFA", "Russia": "UEFA",
    "Republic of Ireland": "UEFA", "Northern Ireland": "UEFA", "Slovakia": "UEFA",
    "Greece": "UEFA", "Slovenia": "UEFA", "Albania": "UEFA", "Bosnia and Herzegovina": "UEFA",
    "Iceland": "UEFA", "Finland": "UEFA", "Bulgaria": "UEFA", "Israel": "UEFA",
    # CONMEBOL
    "Brazil": "CONMEBOL", "Argentina": "CONMEBOL", "Uruguay": "CONMEBOL",
    "Colombia": "CONMEBOL", "Chile": "CONMEBOL", "Peru": "CONMEBOL",
    "Ecuador": "CONMEBOL", "Paraguay": "CONMEBOL", "Bolivia": "CONMEBOL",
    "Venezuela": "CONMEBOL",
    # CAF
    "Senegal": "CAF", "Morocco": "CAF", "Egypt": "CAF", "Nigeria": "CAF",
    "Tunisia": "CAF", "Algeria": "CAF", "Ghana": "CAF", "Ivory Coast": "CAF",
    "Cameroon": "CAF", "South Africa": "CAF", "Mali": "CAF", "Cape Verde": "CAF",
    "DR Congo": "CAF", "Burkina Faso": "CAF",
    # AFC
    "Japan": "AFC", "South Korea": "AFC", "Iran": "AFC", "Australia": "AFC",
    "Saudi Arabia": "AFC", "Qatar": "AFC", "Iraq": "AFC", "United Arab Emirates": "AFC",
    "Uzbekistan": "AFC", "China PR": "AFC", "Jordan": "AFC",
    # CONCACAF
    "United States": "CONCACAF", "Mexico": "CONCACAF", "Canada": "CONCACAF",
    "Costa Rica": "CONCACAF", "Jamaica": "CONCACAF", "Panama": "CONCACAF",
    "Honduras": "CONCACAF", "El Salvador": "CONCACAF", "Curacao": "CONCACAF",
    # OFC
    "New Zealand": "OFC",
}


def confederation_for(team: str) -> str:
    return CONFEDERATIONS.get(team, "UNKNOWN")


def tournament_category(name: str) -> str:
    return TOURNAMENT_MAP.get(name, "FRIENDLY")


def _is_nan(x: Any) -> bool:
    return isinstance(x, float) and math.isnan(x)


class HistoricalResultsScraper:
    name = "historical_results"
    rate_limit = RateLimit(requests=1, per_seconds=60)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=30))
    def fetch(self, *, since: datetime | None = None) -> list[dict[str, Any]]:
        resp = requests.get(CSV_URL, timeout=60)
        resp.raise_for_status()
        df = pd.read_csv(StringIO(resp.text))
        cutoff = since.date().isoformat() if since else TRAINING_START
        df = df[df["date"] >= cutoff].copy()
        return df.to_dict(orient="records")

    def upsert(self, records: list[dict[str, Any]], conn: sqlite3.Connection) -> None:
        cur = conn.cursor()
        cur.execute("SELECT team_id, name FROM teams")
        name_to_id: dict[str, int] = {row["name"]: row["team_id"] for row in cur.fetchall()}
        next_id = (max(name_to_id.values()) + 1) if name_to_id else 1

        # First pass: ensure every team referenced exists in the teams table.
        for rec in records:
            for tname in (rec["home_team"], rec["away_team"]):
                if tname not in name_to_id:
                    cur.execute(
                        "INSERT INTO teams (team_id, name, confederation) VALUES (?, ?, ?)",
                        (next_id, tname, confederation_for(tname)),
                    )
                    name_to_id[tname] = next_id
                    next_id += 1

        # Second pass: upsert matches. INSERT OR IGNORE keeps re-runs idempotent
        # against the UNIQUE(date, home_id, away_id) constraint.
        for rec in records:
            scored = not (_is_nan(rec["home_score"]) or _is_nan(rec["away_score"]))
            cur.execute(
                """INSERT OR IGNORE INTO matches
                   (date, home_id, away_id, tournament,
                    home_goals, away_goals, neutral, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    rec["date"],
                    name_to_id[rec["home_team"]],
                    name_to_id[rec["away_team"]],
                    tournament_category(rec["tournament"]),
                    int(rec["home_score"]) if scored else None,
                    int(rec["away_score"]) if scored else None,
                    1 if bool(rec["neutral"]) else 0,
                    "final" if scored else "scheduled",
                ),
            )
