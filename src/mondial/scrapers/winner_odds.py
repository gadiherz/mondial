"""Winner (Israeli TOTO) 1X2 odds via the bankerim.co.il aggregator.

Winner is the ONLY legal sports book in Israel, so its fixed 1X2 odds are
effectively the official WINNER-16 reference the project evaluates against
(ARCHITECTURE D2). The official winner.co.il `publicapi` exists but sits behind
Imperva Incapsula bot protection (works from an Israeli browser, but 403s scripted
requests and would be blocked harder from GitHub Actions datacenter IPs) -- i.e.
incompatible with deploy-and-forget. So we read the SAME line from the
bankerim.co.il aggregator, which mirrors Winner's line server-rendered, with no bot
wall (datacenter-friendly). It is a third-party mirror, not the source of truth, so
treat it as a Winner *proxy* and spot-check against the official ticket.

Markup contract (per match, a `<div class="game ...">` card):
  data-sportid="2"           football (filter: only sport 2)
  data-event-type="2X1"      the 1X2 full-match market (excludes handicaps/halves)
  <span class="time" data-time="YYYY-MM-DD HH:MM:SS">
  <span class="league">...   competition (Hebrew)
  <span class="desc">HOME - AWAY   team names (Hebrew, " - " separated)
  <span class="bet-home"> O1 ; <span class="bet-x"> OX ; <span class="bet-guest"> O2

Hebrew team names are mapped to the DB's English names via HE_TEAM_ALIASES, then
resolved to DB matches by the existing odds_api resolver (team pair +/-1 day).
Unresolved Hebrew names are logged loudly (never silently dropped) so the alias
table can be extended as live rounds surface new spellings -- same pattern as
odds_api.NAME_ALIASES.
"""
from __future__ import annotations

import logging
import re
import sqlite3
from datetime import datetime, UTC
from typing import Any

import requests
from tenacity import retry, stop_after_attempt, wait_exponential

from mondial.scrapers.base import RateLimit
from mondial.scrapers.odds_api import _norm_index, resolve_match, resolve_team_id

log = logging.getLogger("winner_odds")

# bankerim aggregator pages (URL-encoded Hebrew slugs).
WINNER16_URL = ("https://www.bankerim.co.il/%D7%9E%D7%A9%D7%97%D7%A7%D7%99%D7%9D/"
                "%D7%95%D7%95%D7%99%D7%A0%D7%A8-16.html")
WINNER_LINE_URL = ("https://www.bankerim.co.il/%D7%9E%D7%A9%D7%97%D7%A7%D7%99%D7%9D/"
                   "%D7%95%D7%95%D7%99%D7%A0%D7%A8-%D7%9C%D7%99%D7%99%D7%9F-"
                   "%D7%AA%D7%95%D7%9B%D7%A0%D7%99%D7%95%D7%AA.html")
SPORT_FOOTBALL = "2"      # data-sportid for football
MARKET_1X2 = "2X1"        # data-event-type for the full-match 1/X/2 market

# Hebrew national-team name -> canonical DB (Jurisoo) English name. Values MUST
# match the teams table so the English resolver can map them. Extend as live
# rounds surface new spellings (unresolved names are logged). Covers the WC-2026
# field + common nations that appear in Winner's line.
HE_TEAM_ALIASES: dict[str, str] = {
    # UEFA
    "אנגליה": "England", "צרפת": "France", "ספרד": "Spain", "גרמניה": "Germany",
    "איטליה": "Italy", "פורטוגל": "Portugal", "הולנד": "Netherlands", "בלגיה": "Belgium",
    "קרואטיה": "Croatia", "שווייץ": "Switzerland", "דנמרק": "Denmark", "פולין": "Poland",
    "שבדיה": "Sweden", "אוסטריה": "Austria", "אוקראינה": "Ukraine", "נורווגיה": "Norway",
    "וויילס": "Wales", "ויילס": "Wales", "סקוטלנד": "Scotland", "צ'כיה": "Czech Republic",
    "סרביה": "Serbia", "הונגריה": "Hungary", "טורקיה": "Turkey", "רומניה": "Romania",
    "רוסיה": "Russia", "אירלנד": "Republic of Ireland", "צפון אירלנד": "Northern Ireland",
    "נורבגיה": "Norway",  # site spelling variant of נורווגיה
    "סלובקיה": "Slovakia", "סלובניה": "Slovenia", "אלבניה": "Albania", "יוון": "Greece",
    "בוסניה": "Bosnia and Herzegovina", "בוסניה והרצגובינה": "Bosnia and Herzegovina",
    "איסלנד": "Iceland", "פינלנד": "Finland", "בולגריה": "Bulgaria", "ישראל": "Israel",
    # CONMEBOL
    "ברזיל": "Brazil", "ארגנטינה": "Argentina", "אורוגוואי": "Uruguay",
    "קולומביה": "Colombia", "צ'ילה": "Chile", "פרו": "Peru", "אקוודור": "Ecuador",
    "פרגוואי": "Paraguay", "בוליביה": "Bolivia", "ונצואלה": "Venezuela",
    # CAF
    "סנגל": "Senegal", "מרוקו": "Morocco", "מצרים": "Egypt", "ניגריה": "Nigeria",
    "תוניסיה": "Tunisia", "טוניסיה": "Tunisia",  # both ת/ט spellings appear
    "אלג'יריה": "Algeria", "גאנה": "Ghana", "חוף השנהב": "Ivory Coast",
    "קמרון": "Cameroon", "דרום אפריקה": "South Africa", "מאלי": "Mali", "כף ורדה": "Cape Verde",
    "קונגו": "DR Congo", "בורקינה פאסו": "Burkina Faso",
    "הרפובליקה הדמוקרטית של קונגו": "DR Congo",  # site's full name for קונגו
    # AFC
    "יפן": "Japan", "דרום קוריאה": "South Korea", "איראן": "Iran", "אוסטרליה": "Australia",
    "אירן": "Iran",  # site spelling variant of איראן
    "ערב הסעודית": "Saudi Arabia", "קטאר": "Qatar", "עיראק": "Iraq",
    "עירק": "Iraq",  # site spelling variant of עיראק
    "איחוד האמירויות": "United Arab Emirates", "אוזבקיסטן": "Uzbekistan",
    "אוזבקיסטאן": "Uzbekistan",  # site spelling variant of אוזבקיסטן
    "סין": "China PR", "ירדן": "Jordan",
    # CONCACAF / OFC
    "ארהב": "United States", "ארצות הברית": "United States", "מקסיקו": "Mexico",
    "קנדה": "Canada", "קוסטה ריקה": "Costa Rica", "ג'מייקה": "Jamaica", "פנמה": "Panama",
    "הונדורס": "Honduras", "אל סלבדור": "El Salvador", "האיטי": "Haiti",
    "קוראסאו": "Curacao", "קורסאו": "Curacao", "קוראקאו": "Curacao",
    "ניו זילנד": "New Zealand",
}

_DROP = str.maketrans("", "", "\"'`׳״’‘")  # geresh/gershayim/quotes


def _he_norm(name: str) -> str:
    """Light Hebrew normaliser: drop quote-like marks (incl. geresh/gershayim),
    collapse whitespace. Applied to BOTH the alias keys and incoming names so a
    geresh spelling like צ'ילה matches regardless of which mark the source uses."""
    return re.sub(r"\s+", " ", name.translate(_DROP)).strip()


# Normalised lookup so keys and inputs are compared on the same footing.
_HE_LOOKUP: dict[str, str] = {_he_norm(k): v for k, v in HE_TEAM_ALIASES.items()}


def he_to_english(he_name: str) -> str | None:
    """Map a Hebrew team name to its DB English name (or None if unknown)."""
    return _HE_LOOKUP.get(_he_norm(he_name))


# --- parsing ---------------------------------------------------------------
_CARD = re.compile(r'<div class="game\b[^>]*>')
_RE = {
    "sportid": re.compile(r'data-sportid="(\d+)"'),
    "event_type": re.compile(r'data-event-type="([^"]+)"'),
    "time": re.compile(r'data-time="([^"]+)"'),
    "league": re.compile(r'class="league"[^>]*>([^<]+)<'),
    "desc": re.compile(r'class="desc"[^>]*>([^<]+)<'),
}


def _odd(card: str, cls: str) -> float | None:
    i = card.find(f'class="{cls}')
    if i < 0:
        return None
    m = re.search(r"(\d{1,2}\.\d{2})", card[i:i + 170])
    return float(m.group(1)) if m else None


def parse_cards(html: str) -> list[dict[str, Any]]:
    """Parse bankerim game cards into football 1X2 odds records.

    Keeps only sport=football (data-sportid=2) and the full-match 1X2 market
    (data-event-type=2X1) with a clean two-team `desc` and all three odds.
    Returns dicts: {commence, league, home_he, away_he, odd_home, odd_draw, odd_away}.
    """
    starts = [m.start() for m in _CARD.finditer(html)]
    out: list[dict[str, Any]] = []
    for k, s in enumerate(starts):
        card = html[s:(starts[k + 1] if k + 1 < len(starts) else len(html))]
        sport = _RE["sportid"].search(card)
        etype = _RE["event_type"].search(card)
        if not sport or sport.group(1) != SPORT_FOOTBALL:
            continue
        if not etype or MARKET_1X2 not in etype.group(1):
            continue
        desc = _RE["desc"].search(card)
        tm = _RE["time"].search(card)
        if not desc or not tm:
            continue
        teams = desc.group(1).strip()
        # Plain full match only: skip derivative markets that annotate the teams
        # with a parenthetical (handicap "(1+)", half "(מחצית)", minutes "(60 דק')").
        if "(" in teams or " - " not in teams:
            continue
        home_he, away_he = (p.strip() for p in teams.split(" - ", 1))
        oh, od, oa = _odd(card, "bet-home"), _odd(card, "bet-x"), _odd(card, "bet-guest")
        if None in (oh, od, oa):
            continue
        lg = _RE["league"].search(card)
        out.append({
            "commence": tm.group(1), "league": lg.group(1).strip() if lg else None,
            "home_he": home_he, "away_he": away_he,
            "odd_home": oh, "odd_draw": od, "odd_away": oa,
        })
    return out


class WinnerOddsScraper:
    name = "winner_odds"
    rate_limit = RateLimit(requests=60, per_seconds=60)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=30))
    def _get(self, url: str) -> str:
        resp = requests.get(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept-Language": "he-IL,he;q=0.9,en;q=0.8"}, timeout=30)
        resp.raise_for_status()
        return resp.text

    def fetch(self, *, line: bool = True) -> list[dict[str, Any]]:
        """Fetch + parse the Winner-16 coupon and (optionally) the full Winner Line.

        Both pages are merged and de-duplicated by (commence-date, teams); the
        coupon is a curated subset of the line, so this just widens coverage.
        """
        records: list[dict[str, Any]] = []
        urls = [WINNER16_URL] + ([WINNER_LINE_URL] if line else [])
        for url in urls:
            try:
                records += parse_cards(self._get(url))
            except requests.RequestException as e:
                log.warning("winner_odds: fetch failed for %s: %s", url, e)
        # de-dup by (date, home_he, away_he)
        seen, deduped = set(), []
        for r in records:
            key = (r["commence"][:10], r["home_he"], r["away_he"])
            if key not in seen:
                seen.add(key)
                deduped.append(r)
        log.info("winner_odds: parsed %d football 1X2 matches (%d raw)",
                 len(deduped), len(records))
        return deduped

    def upsert(self, records: list[dict[str, Any]], conn: sqlite3.Connection) -> None:
        """Resolve each Hebrew match to a DB fixture and write a `winner` odds row.

        Idempotent per (match_id, 'winner'): the row is cleared before re-insert,
        so re-runs refresh rather than duplicate. Unresolved Hebrew names and
        unmatched fixtures are logged loudly, never silently dropped.
        """
        idx = _norm_index(conn)
        ts_now = datetime.now(UTC).isoformat()
        written = skipped_name = skipped_match = 0
        unresolved: set[str] = set()

        for r in records:
            en_home = he_to_english(r["home_he"])
            en_away = he_to_english(r["away_he"])
            if en_home is None or en_away is None:
                if en_home is None:
                    unresolved.add(r["home_he"])
                if en_away is None:
                    unresolved.add(r["away_he"])
                skipped_name += 1
                continue
            h_id = resolve_team_id(en_home, idx)
            a_id = resolve_team_id(en_away, idx)
            if h_id is None or a_id is None:
                skipped_name += 1
                continue
            commence = datetime.fromisoformat(r["commence"]).replace(tzinfo=UTC)
            m = resolve_match(conn, h_id, a_id, commence)
            if m is None:
                skipped_match += 1
                continue
            # Orient to OUR DB home/away (the aggregator may differ for neutral venues).
            db_home_is_winner_home = m["home_id"] == h_id
            odd_home = r["odd_home"] if db_home_is_winner_home else r["odd_away"]
            odd_away = r["odd_away"] if db_home_is_winner_home else r["odd_home"]
            conn.execute("DELETE FROM odds_snapshots WHERE match_id=? AND bookmaker=?",
                         (m["match_id"], "winner"))
            conn.execute(
                """INSERT INTO odds_snapshots
                   (match_id, bookmaker, ts, odd_home, odd_draw, odd_away)
                   VALUES (?, 'winner', ?, ?, ?, ?)""",
                (m["match_id"], ts_now, odd_home, r["odd_draw"], odd_away))
            written += 1

        if unresolved:
            log.warning("winner_odds: %d unresolved Hebrew team name(s) -> add to "
                        "HE_TEAM_ALIASES: %s", len(unresolved), sorted(unresolved))
        log.info("winner_odds upsert: wrote %d 'winner' rows; skipped %d (unknown "
                 "team), %d (no DB match)", written, skipped_name, skipped_match)
