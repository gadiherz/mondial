"""The Odds API scraper -- live HDA (1X2) odds for WC 2026 matches.

Docs: https://the-odds-api.com/liveapi/guides/v4/
Sport key for the tournament: 'soccer_fifa_world_cup'. For soccer the `h2h`
market returns THREE outcomes -- home team, away team, and "Draw" -- i.e. the
full Home/Draw/Away line the value picker needs.

Free tier: 500 credits/month. Cost per call = (#regions) x (#markets), so we
default to a single region ('eu', which carries Pinnacle, our sharp reference)
and the single 'h2h' market => 1 credit per fetch.

Each (event, bookmaker) becomes one `odds_snapshots` row, oriented to OUR DB's
home/away ordering (the feed's home/away may be swapped for neutral venues, so
we map by team identity, not by position). Anything we can't resolve to a team
or a scheduled match is logged and skipped -- never silently dropped.
"""
from __future__ import annotations

import logging
import re
import sqlite3
import unicodedata
from datetime import datetime, timedelta, UTC
from typing import Any

import requests
from tenacity import retry, stop_after_attempt, wait_exponential

from mondial.config import settings
from mondial.scrapers.base import RateLimit

log = logging.getLogger("odds_api")

BASE = "https://api.the-odds-api.com/v4"
SPORT_KEY = "soccer_fifa_world_cup"

# The Odds API sometimes names teams differently from the Jurisoo dataset our
# `teams` table is built from. Keys and values are both compared after _norm().
# Extend this as live runs surface unresolved names (they are logged loudly).
NAME_ALIASES: dict[str, str] = {
    "usa": "united states",
    "united states of america": "united states",
    "korea republic": "south korea",
    "south korea": "south korea",
    "cote divoire": "ivory coast",
    "czechia": "czech republic",
    "turkiye": "turkey",
    "cabo verde": "cape verde",
    "democratic republic of the congo": "dr congo",
    "congo dr": "dr congo",
    "dr congo": "dr congo",
    "bosnia herzegovina": "bosnia and herzegovina",
    "ir iran": "iran",
}


def _norm(name: str) -> str:
    """Accent-strip, lowercase, keep alphanumerics+spaces, collapse whitespace."""
    nfkd = unicodedata.normalize("NFKD", name)
    ascii_ = "".join(c for c in nfkd if not unicodedata.combining(c))
    cleaned = re.sub(r"[^a-z0-9 ]+", " ", ascii_.lower())
    return re.sub(r"\s+", " ", cleaned).strip()


def _norm_index(conn: sqlite3.Connection) -> dict[str, int]:
    """Normalised-team-name -> team_id, from the teams table."""
    return {_norm(r["name"]): r["team_id"]
            for r in conn.execute("SELECT team_id, name FROM teams")}


def resolve_team_id(api_name: str, norm_index: dict[str, int]) -> int | None:
    """Map an Odds-API team name to our team_id, via alias table then normalised name."""
    n = _norm(api_name)
    n = _norm(NAME_ALIASES.get(n, n))
    return norm_index.get(n)


def resolve_match(
    conn: sqlite3.Connection, home_id: int, away_id: int, commence: datetime,
) -> sqlite3.Row | None:
    """Find the DB match for this unordered team pair near the kickoff date.

    Matches the unordered {home_id, away_id} pair within +/-1 day of the feed's
    commence date (UTC), preferring a still-scheduled fixture and the closest
    date. Returns the row (match_id, home_id, away_id) or None.
    """
    cdate = commence.date()
    rows = conn.execute(
        """SELECT match_id, home_id, away_id, date, status FROM matches
           WHERE ((home_id=? AND away_id=?) OR (home_id=? AND away_id=?))
             AND date BETWEEN ? AND ?""",
        (home_id, away_id, away_id, home_id,
         (cdate - timedelta(days=1)).isoformat(),
         (cdate + timedelta(days=1)).isoformat()),
    ).fetchall()
    if not rows:
        return None
    return min(
        rows,
        key=lambda r: (r["status"] != "scheduled",
                       abs((datetime.fromisoformat(r["date"]).date() - cdate).days)),
    )


def record_kickoff(conn: sqlite3.Connection, match_id: int, commence: datetime) -> None:
    """Store a fixture's real kickoff time (UTC ISO) from an Odds API event.

    The DB otherwise holds only the match date; the results scanner needs the
    actual kickoff to know when a match should have finished. Captured for free
    from the commence_time already present in every odds/scores event.
    """
    conn.execute("UPDATE matches SET kickoff_utc=? WHERE match_id=?",
                 (commence.astimezone(UTC).isoformat().replace("+00:00", "Z"), match_id))


class OddsAPIScraper:
    name = "odds_api"
    rate_limit = RateLimit(requests=500, per_seconds=30 * 24 * 3600)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=30))
    def fetch(
        self, *, since: datetime | None = None, regions: str = "eu",
    ) -> list[dict[str, Any]]:
        """Fetch live h2h (HDA) odds for the WC. `regions='eu'` includes Pinnacle.

        Returns [] (with a warning) if the sport key is not currently active
        (The Odds API answers 404/422 outside a competition's market window),
        so the daily pipeline degrades gracefully before match markets open.
        """
        if not settings.odds_api_key:
            raise RuntimeError(
                "ODDS_API_KEY not set. Get a free key at the-odds-api.com and "
                "put it in .env (free tier = 500 credits/month)."
            )
        url = f"{BASE}/sports/{SPORT_KEY}/odds"
        params = {
            "apiKey": settings.odds_api_key,
            "regions": regions,
            "markets": "h2h",
            "oddsFormat": "decimal",
        }
        resp = requests.get(url, params=params, timeout=30)
        if resp.status_code in (404, 422):
            log.warning(
                "odds_api: sport %s not active yet (HTTP %d) -- no WC markets "
                "open. Returning 0 events.", SPORT_KEY, resp.status_code,
            )
            return []
        resp.raise_for_status()
        remaining = resp.headers.get("x-requests-remaining")
        if remaining is not None:
            log.info("odds_api: %s credits remaining this month", remaining)
        return resp.json()

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=30))
    def fetch_scores(self, *, days_from: int = 3) -> list[dict[str, Any]]:
        """Recent/live/completed WC scores (Routine B). `days_from` max 3.

        Returns [] (with a warning) if the sport key isn't active yet.
        """
        if not settings.odds_api_key:
            raise RuntimeError("ODDS_API_KEY not set.")
        url = f"{BASE}/sports/{SPORT_KEY}/scores"
        resp = requests.get(url, params={
            "apiKey": settings.odds_api_key, "daysFrom": days_from,
            "dateFormat": "iso"}, timeout=30)
        if resp.status_code in (404, 422):
            log.warning("odds_api scores: sport %s not active (HTTP %d).",
                        SPORT_KEY, resp.status_code)
            return []
        resp.raise_for_status()
        return resp.json()

    def upsert(self, records: list[dict[str, Any]], conn: sqlite3.Connection) -> None:
        norm_index = _norm_index(conn)
        ts_now = datetime.now(UTC).isoformat()
        written = skipped_team = skipped_match = no_h2h = 0

        for ev in records:
            ev_home, ev_away = ev.get("home_team"), ev.get("away_team")
            h_id = resolve_team_id(ev_home or "", norm_index)
            a_id = resolve_team_id(ev_away or "", norm_index)
            if h_id is None or a_id is None:
                log.warning("odds_api: unresolved team(s) %r / %r -- skipping "
                            "event; add to NAME_ALIASES if recurring.",
                            ev_home, ev_away)
                skipped_team += 1
                continue

            commence = datetime.fromisoformat(
                ev["commence_time"].replace("Z", "+00:00"))
            m = resolve_match(conn, h_id, a_id, commence)
            if m is None:
                log.warning("odds_api: no DB match for %s vs %s near %s -- "
                            "skipping.", ev_home, ev_away, commence.date())
                skipped_match += 1
                continue

            record_kickoff(conn, m["match_id"], commence)  # capture real kickoff time
            db_home_is_ev_home = m["home_id"] == h_id  # else feed is swapped

            for bk in ev.get("bookmakers", []):
                mkt = next((x for x in bk.get("markets", [])
                            if x.get("key") == "h2h"), None)
                if mkt is None:
                    continue
                ev_home_price = ev_away_price = draw_price = None
                for oc in mkt.get("outcomes", []):
                    nm = oc.get("name", "")
                    price = oc.get("price")
                    if _norm(nm) == "draw":
                        draw_price = price
                    elif resolve_team_id(nm, norm_index) == h_id:
                        ev_home_price = price
                    elif resolve_team_id(nm, norm_index) == a_id:
                        ev_away_price = price
                if None in (ev_home_price, ev_away_price, draw_price):
                    no_h2h += 1
                    continue

                odd_home = ev_home_price if db_home_is_ev_home else ev_away_price
                odd_away = ev_away_price if db_home_is_ev_home else ev_home_price
                conn.execute(
                    """INSERT INTO odds_snapshots
                       (match_id, bookmaker, ts, odd_home, odd_draw, odd_away)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (m["match_id"], bk.get("key", "unknown"),
                     bk.get("last_update", ts_now),
                     odd_home, draw_price, odd_away),
                )
                written += 1

        log.info("odds_api upsert: %d snapshot rows; skipped %d (unknown team), "
                 "%d (no match), %d (incomplete h2h)",
                 written, skipped_team, skipped_match, no_h2h)
