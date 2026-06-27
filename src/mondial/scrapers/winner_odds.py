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

Markup contract (per match, a `<div class="game is-father ...">` card):
  data-sportid="2"           football (filter: only sport 2)
  data-event-type="2X1"      the 1X2 full-match market (excludes handicaps/halves)
  <span ... data-original-title="HH:MM DD.MM.YYYY" class="time">STATUS</span>
                             kickoff datetime now lives in the time span's tooltip
                             (Israel local); the span TEXT is the live status
                             (kickoff time / minute / "הסתיים"). It used to be a
                             `data-time="YYYY-MM-DD HH:MM:SS"` attribute (2026-06
                             aggregator redesign).
  <span class="desc">HOME - AWAY   team names (Hebrew, " - " separated)
  <span class="bet-home win"> O1 ; <span class="bet-x "> OX ; <span class="bet-guest "> O2
                             odds now nested in a <span class="box-colors"> wrapper;
                             _odd() still finds the first NN.NN after the class.

The competition/league is no longer per-card; it is a `data-league` attribute on
the section headline row, so `league` is dropped from the record (it was only
informational). The WINNER-16 coupon page is now client-rendered (returns no cards
to a scripted GET), so the full Winner-Line page is the effective source; both are
still fetched and a 0-card parse is logged loudly as a breakage canary.

Hebrew team names are mapped to the DB's English names via HE_TEAM_ALIASES, then
resolved to DB matches by the existing odds_api resolver (team pair +/-1 day).
Unresolved Hebrew names are logged loudly (never silently dropped) so the alias
table can be extended as live rounds surface new spellings -- same pattern as
odds_api.NAME_ALIASES.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import datetime, UTC
from typing import Any

import requests
from tenacity import retry, stop_after_attempt, wait_exponential

from mondial.config import DATA_DIR
from mondial.scrapers.base import RateLimit
from mondial.scrapers.odds_api import _norm_index, resolve_match, resolve_team_id

log = logging.getLogger("winner_odds")

# --- Cloudflare-fetched cache (CI's primary source) ---------------------------
# bankerim.co.il geo/bot-blocks GitHub Actions datacenter IPs (the root cause of
# the 2026-06 Winner-odds staleness: a local Israeli IP scraped fine, CI got 0
# rows, and the failure committed green). So the page is fetched by the Cloudflare
# worker (served from an allowed network) and the raw HTML committed here; CI parses
# the cache offline with the SAME parser -- no live network on the runner. The live
# fetch below stays as a fallback for local/manual runs from an IL/residential IP.
# See cron-worker/worker.js + pipelines/refresh_winner.verify_winner_fresh.
CACHE_DIR = DATA_DIR / "winner_cache"
CACHE_FILES = {"coupon": "bankerim_coupon.html", "line": "bankerim_line.html"}
CACHE_META = "meta.json"


def read_cache_meta() -> dict | None:
    """The worker's cache manifest ({fetched_at, results:[{url,http_status,bytes}]})
    or None if it has never been written. Used by the freshness gate."""
    p = CACHE_DIR / CACHE_META
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None

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
    "קייפ ורדה": "Cape Verde",  # site spelling variant of כף ורדה
    "קאפו ורדה": "Cape Verde",  # site spelling variant of כף ורדה
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
    # Kickoff datetime moved from a `data-time` attribute to the tooltip
    # (`data-original-title`, "HH:MM DD.MM.YYYY", Israel local) of the status
    # `<span class="time">`. It is present on every card -- scheduled, in-play AND
    # finished -- so it is now MORE reliable than the old data-time (which finished
    # cards dropped). Anchoring on the trailing `class="time"` keeps the round-id
    # tooltip ("תוכניה מס' ...") on the same card from being mistaken for it.
    "time": re.compile(
        r'data-original-title="(\d{1,2}:\d{2}\s+\d{1,2}\.\d{1,2}\.\d{4})"[^>]*class="time"'),
    "desc": re.compile(r'class="desc"[^>]*>([^<]+)<'),
}


def _parse_dt(raw: str) -> str | None:
    """bankerim's kickoff tooltip is 'HH:MM DD.MM.YYYY' (Israel local). Convert to
    an ISO 'YYYY-MM-DDTHH:MM:00' string the upsert resolver can parse. The exact
    timezone is irrelevant here: resolve_match windows the commence date +/-1 day,
    so a 3h IDT/UTC offset never changes which fixture it lands on."""
    try:
        return datetime.strptime(re.sub(r"\s+", " ", raw).strip(),
                                 "%H:%M %d.%m.%Y").isoformat()
    except ValueError:
        return None


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
    Returns dicts: {commence, home_he, away_he, odd_home, odd_draw, odd_away}.
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
        if not desc:
            continue
        # The kickoff tooltip is present on every card (scheduled, in-play AND
        # finished), so `commence` is normally populated; it falls back to None only
        # if the datetime fails to parse, in which case upsert resolves by team pair.
        # Overwrite protection for already-priced finished matches now keys on the DB
        # status in upsert (not on commence), so a finished card's closing line never
        # clobbers a pre-kickoff line.
        teams = desc.group(1).strip()
        # Plain full match only: skip derivative markets that annotate the teams
        # with a parenthetical (handicap "(1+)", half "(מחצית)", minutes "(60 דק')").
        if "(" in teams or " - " not in teams:
            continue
        home_he, away_he = (p.strip() for p in teams.split(" - ", 1))
        oh, od, oa = _odd(card, "bet-home"), _odd(card, "bet-x"), _odd(card, "bet-guest")
        if None in (oh, od, oa):
            continue
        out.append({
            "commence": _parse_dt(tm.group(1)) if tm else None,
            "home_he": home_he, "away_he": away_he,
            "odd_home": oh, "odd_draw": od, "odd_away": oa,
        })
    return out


def _dedup(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """De-dup parsed cards by (commence-date, home_he, away_he).

    The coupon is a curated subset of the line, and the cache holds both pages, so
    merging them double-lists shared fixtures -- collapse to one record each.
    """
    seen, out = set(), []
    for r in records:
        key = ((r["commence"] or "")[:10], r["home_he"], r["away_he"])
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


def _resolve_pair(conn: sqlite3.Connection, home_id: int, away_id: int) -> sqlite3.Row | None:
    """Resolve a WC fixture by unordered team pair when the card carries no date.

    Finished/in-play bankerim cards drop `data-time`, so `resolve_match` (which
    windows on the commence date) can't be used. Scope to tournament='WC' and
    prefer a still-scheduled fixture, then the one nearest today -- this keeps the
    line off the historical 2014/2018/2022 editions (years from now) that share
    the same `tournament='WC'` tag.
    """
    rows = conn.execute(
        """SELECT match_id, home_id, away_id, date, status FROM matches
           WHERE tournament='WC' AND
                 ((home_id=? AND away_id=?) OR (home_id=? AND away_id=?))""",
        (home_id, away_id, away_id, home_id),
    ).fetchall()
    if not rows:
        return None
    today = datetime.now(UTC).date()
    return min(
        rows,
        key=lambda r: (r["status"] != "scheduled",
                       abs((datetime.fromisoformat(r["date"]).date() - today).days)),
    )


class WinnerOddsScraper:
    name = "winner_odds"
    rate_limit = RateLimit(requests=60, per_seconds=60)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=30))
    def _get(self, url: str) -> str:
        resp = requests.get(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept-Language": "he-IL,he;q=0.9,en;q=0.8"}, timeout=30)
        # Log status + body size BEFORE raise_for_status so a bot/geo wall (403/429
        # or a tiny challenge page served only to datacenter IPs) is visible in the
        # GitHub Actions log -- it distinguishes "blocked" from "markup drifted".
        log.info("winner_odds: GET %s -> HTTP %s, %d bytes",
                 url, resp.status_code, len(resp.content))
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
                html = self._get(url)
            except requests.RequestException as e:
                log.warning("winner_odds: fetch failed for %s: %s", url, e)
                continue
            page = parse_cards(html)
            # Per-URL diagnostics so a future breakage is pinpointed from the log
            # alone: cards present but 0 parsed => markup drift; cards + parsed but
            # 0 resolved => the page lists no national-team fixtures yet (the line
            # is not posted at this run time -- a scheduling miss, not a code bug).
            n_cards = len(_CARD.findall(html))
            n_market = html.count(MARKET_1X2)
            n_resolved = sum(
                1 for r in page
                if he_to_english(r["home_he"]) and he_to_english(r["away_he"]))
            log.info("winner_odds: %s -> %d game cards, %d %s markets, %d parsed, "
                     "%d resolved-to-national-team", url, n_cards, n_market,
                     MARKET_1X2, len(page), n_resolved)
            if not page:
                log.warning(
                    "winner_odds: 0 football 1X2 cards parsed from %s -- the "
                    "aggregator markup may have changed again, or this page is "
                    "now client-rendered (the WINNER-16 coupon is). The full "
                    "Winner-Line page is the working source; see parse_cards.",
                    url)
            records += page
        deduped = _dedup(records)
        log.info("winner_odds: parsed %d football 1X2 matches (%d raw)",
                 len(deduped), len(records))
        return deduped

    def fetch_cached(self) -> list[dict[str, Any]]:
        """Parse the Cloudflare-committed bankerim HTML cache (CI's primary source).

        Identical parser to `fetch`, but the HTML comes from data/winner_cache/
        (committed by the worker from an allowed network) instead of a live GET --
        because bankerim blocks the GitHub Actions runner IP. Returns [] (logged
        loudly) if no cache file exists yet. See module docstring + worker.js.
        """
        records: list[dict[str, Any]] = []
        found = False
        for role, fname in CACHE_FILES.items():
            p = CACHE_DIR / fname
            if not p.exists():
                log.warning("winner_odds: cache file missing: %s", p)
                continue
            found = True
            html = p.read_text(encoding="utf-8", errors="replace")
            page = parse_cards(html)
            n_cards = len(_CARD.findall(html))
            n_resolved = sum(
                1 for r in page
                if he_to_english(r["home_he"]) and he_to_english(r["away_he"]))
            log.info("winner_odds[cache:%s]: %d game cards, %d parsed, %d "
                     "resolved-to-national-team (%d bytes)",
                     role, n_cards, len(page), n_resolved, len(html))
            records += page
        if not found:
            log.warning(
                "winner_odds: NO cache files under %s -- the Cloudflare worker has "
                "not committed a bankerim page yet (or the deploy path is wrong). "
                "The freshness gate will fail loudly if a near match is unpriced. "
                "See cron-worker/worker.js.", CACHE_DIR)
        return _dedup(records)

    def fetch_records(self, *, prefer_cache: bool = True, line: bool = True
                      ) -> list[dict[str, Any]]:
        """Winner records, cache-first (CI) with a live fallback (local IL runs).

        CI MUST use the cache: bankerim blocks the runner IP, so a live GET returns
        nothing there. A local/manual run from an Israeli/residential IP can still
        fetch live, so if the cache is absent/empty we fall back to `fetch` (which is
        logged when it then fails from a blocked IP). The verification gate, not this
        method, is what turns a persistent miss into a hard failure.
        """
        if prefer_cache:
            cached = self.fetch_cached()
            if cached:
                return cached
            log.warning("winner_odds: cache empty -> trying a live fetch "
                        "(works from an IL/residential IP; blocked from CI).")
        return self.fetch(line=line)

    def upsert(self, records: list[dict[str, Any]], conn: sqlite3.Connection) -> None:
        """Resolve each Hebrew match to a DB fixture and write a `winner` odds row.

        Idempotent per (match_id, 'winner'): the row is cleared before re-insert,
        so re-runs refresh rather than duplicate. Unresolved Hebrew names and
        unmatched fixtures are logged loudly, never silently dropped.
        """
        idx = _norm_index(conn)
        ts_now = datetime.now(UTC).isoformat()
        written = skipped_name = skipped_match = skipped_settled = 0
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
            if r["commence"] is not None:
                commence = datetime.fromisoformat(r["commence"]).replace(tzinfo=UTC)
                m = resolve_match(conn, h_id, a_id, commence)
            else:
                # Card with no parseable kickoff tooltip: resolve by team pair alone
                # so the line still prices the match for settlement + Results.
                m = _resolve_pair(conn, h_id, a_id)
            if m is None:
                skipped_match += 1
                continue
            if m["status"] == "final" and conn.execute(
                    "SELECT 1 FROM odds_snapshots WHERE match_id=? AND bookmaker='winner'",
                    (m["match_id"],)).fetchone():
                # Match already final AND already priced pre-kickoff -- never overwrite
                # that opening line (the bets settle against it) with the post-hoc
                # closing line. A final match with NO prior Winner row (priced only
                # after an aggregator outage, e.g. Spain v Saudi Arabia 2026-06-21)
                # still falls through below and gets its closing line written, so a
                # match missed during the outage can be back-priced and counted.
                skipped_settled += 1
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
                 "team), %d (no DB match), %d (already priced pre-kickoff)",
                 written, skipped_name, skipped_match, skipped_settled)
