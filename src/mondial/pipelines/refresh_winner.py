"""Routine C — Winner-odds refresh (cheap, frequent, self-healing, LOUD on failure).

Why this exists separate from Routine A (markets_intel): the bankerim Winner line
for a play-day is often posted AFTER Routine A's once-a-day 14:00 UTC run, so that
run prices 0 fixtures and the matches stay unpriced all day. This routine re-attempts
ONLY the Winner scrape (no Odds API, no LLM intel, no re-predict), so it is cheap
enough to run several times a day and to be poked by the Cloudflare worker whenever
upcoming matches still lack a Winner price.

WHERE THE ODDS COME FROM (root cause of the recurring staleness, fixed 2026-06):
bankerim.co.il geo/bot-blocks GitHub Actions datacenter IPs -- a local Israeli IP
scraped fine, but every CI run wrote 0 rows AND committed green, so the feed sat
stale for days while three parser-level "fixes" did nothing (they fixed the wrong
layer). The fix decouples the network hop from the parse: the Cloudflare worker
fetches the bankerim pages from an allowed network and commits the raw HTML to
data/winner_cache/; this routine parses that cache offline (scrapers/winner_odds.
fetch_records, cache-first) and then HARD-VERIFIES the result.

Steps:
  fetch_records()      -> parse the worker-committed cache (live fallback locally)
  upsert(...)          -> write bookmaker='winner' rows for any newly-posted line
  place_virtual_bets() -> place the six-player bets for matches now priced
  publish_web()        -> republish web/data/dashboard.json (clears the staleness
                          canary the frontend shows once odds arrive)
  verify_winner_fresh()-> RAISE (non-zero exit -> RED workflow) if a near upcoming
                          match is still unpriced from a fresh page. This is the
                          guarantee that a stale Winner feed can NEVER again pass as
                          a successful run. NOT @step-wrapped on purpose.

Predictions are NOT regenerated here -- Routine A already wrote them; this only adds
the late Winner line and the bets/feed that depend on it.

Run: python -m mondial.pipelines.refresh_winner
"""
from __future__ import annotations

import logging
import sys
from datetime import UTC, datetime
from typing import Any

from mondial.config import WINNER_CACHE_MAX_AGE_HOURS, WINNER_NEAR_DAYS
from mondial.data.db import connect, init_db
from mondial.eval.odds import upcoming_unpriced_winner
from mondial.pipelines.daily_run import place_virtual_bets, publish_web
from mondial.scrapers.winner_odds import (
    WinnerOddsScraper,
    he_to_english,
    read_cache_meta,
)

log = logging.getLogger("refresh_winner")


def _cache_age_hours(meta: dict | None) -> float | None:
    """Hours since the worker last committed the bankerim cache, or None if never."""
    if not meta or not meta.get("fetched_at"):
        return None
    try:
        dt = datetime.fromisoformat(meta["fetched_at"])
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return (datetime.now(UTC) - dt).total_seconds() / 3600.0


def _resolved_card_for_fixture(records: list[dict[str, Any]], home: str, away: str) -> bool:
    """True if a parsed card resolves (via HE_TEAM_ALIASES) to exactly this fixture's
    two teams. Used to tell 'the page HAD this match but we failed to price it'
    (a real parser/resolver/alias bug -> RED) from 'bankerim has not posted it yet'
    (-> WARN). Matching on the RESOLVED English pair, not a same-day date, keeps an
    unrelated club fixture on the same day from triggering a false failure."""
    pair = {home, away}
    for r in records:
        eh, ea = he_to_english(r["home_he"]), he_to_english(r["away_he"])
        if eh and ea and {eh, ea} == pair:
            return True
    return False


def verify_winner_fresh(records: list[dict[str, Any]]) -> int:
    """Hard gate: a near upcoming match must be Winner-priced from a FRESH page.

    Replaces the old silent-green commit -- the failure mode that let stale Winner
    odds sit unnoticed for days. False-red-free by construction:

      * No near unpriced match                         -> OK (nothing to price).
      * Near unpriced + cache absent/stale             -> RAISE (the worker fetch
        pipeline is down -- the actual root cause: check cron-worker/worker.js egress;
        bankerim blocks CI).
      * Near unpriced + the SAME team-pair is already priced on another DB fixture
                                                        -> WARN, never RED (this row is
        a duplicate/placeholder artifact, not a missed parse -- the price landed on the
        sibling fixture, e.g. a knockout bracket row vs a stray dated copy of the same
        pairing. Guards against a phantom-duplicate hourly email storm).
      * Near unpriced + FRESH cache that lists the match (resolves to its two teams)
        and the pair is NOT priced anywhere
                                                        -> RAISE (we had the page but
        the parser/alias/resolver dropped it -- a real bug to fix).
      * Near unpriced + fresh cache that does NOT list it
                                                        -> WARN (bankerim has not
        posted the line yet; it auto-fills on the next post).

    Returns the count of still-unpriced near matches (0 on the OK path). Raises
    RuntimeError on the two hard-failure paths.
    """
    with connect() as conn:
        unpriced = upcoming_unpriced_winner(conn, WINNER_NEAR_DAYS)
        # Unordered team-pairs that ARE priced somewhere with a complete Winner line.
        # A near-unpriced fixture whose pair is in this set is a duplicate/placeholder
        # (the price landed on the sibling row), NOT a parser miss -- never RED on it.
        priced_pairs = {
            frozenset((r["home"], r["away"]))
            for r in conn.execute(
                """SELECT h.name AS home, a.name AS away FROM odds_snapshots o
                   JOIN matches m ON m.match_id = o.match_id
                   JOIN teams h ON h.team_id = m.home_id
                   JOIN teams a ON a.team_id = m.away_id
                   WHERE o.bookmaker='winner' AND o.odd_home IS NOT NULL
                     AND o.odd_draw IS NOT NULL AND o.odd_away IS NOT NULL""")
        }
    if not unpriced:
        log.info("winner gate OK: no near (<=%dd) upcoming WC match is unpriced.",
                 WINNER_NEAR_DAYS)
        return 0

    names = ", ".join(f"{r['home']} v {r['away']} ({r['date']})" for r in unpriced)
    meta = read_cache_meta()
    age = _cache_age_hours(meta)
    if age is None or age > WINNER_CACHE_MAX_AGE_HOURS:
        state = "absent" if age is None else f"stale ({age:.1f}h > {WINNER_CACHE_MAX_AGE_HOURS}h)"
        raise RuntimeError(
            f"WINNER GATE FAILED: {len(unpriced)} near match(es) unpriced [{names}] "
            f"and the bankerim cache is {state}. The Cloudflare worker is not "
            "committing a fresh page -- check cron-worker/worker.js egress (bankerim "
            "geo-blocks CI IPs; if Cloudflare egress is blocked too, switch to the "
            "residential-proxy fallback). See HANDOFF.")

    # A near-unpriced fixture whose pair is already priced elsewhere is a
    # duplicate/placeholder row (the price landed on its sibling) -- WARN, never RED.
    dup_priced = [f"{r['home']} v {r['away']} ({r['date']})" for r in unpriced
                  if frozenset((r["home"], r["away"])) in priced_pairs]
    if dup_priced:
        log.warning(
            "winner gate: %d near 'unpriced' fixture(s) are duplicate/placeholder rows "
            "whose team-pair is ALREADY priced on a sibling fixture -- ignoring (not a "
            "parser miss): %s. If these persist, a duplicate WC-2026 row may exist; see "
            "scripts/repair_knockout_dupes.py.", len(dup_priced), "; ".join(dup_priced))

    dropped = [f"{r['home']} v {r['away']} ({r['date']})" for r in unpriced
               if _resolved_card_for_fixture(records, r["home"], r["away"])
               and frozenset((r["home"], r["away"])) not in priced_pairs]
    if dropped:
        raise RuntimeError(
            "WINNER GATE FAILED: the FRESH bankerim cache lists these near fixtures "
            f"but they were NOT priced -- a parser/resolver/alias bug to fix: "
            f"{'; '.join(dropped)}. See the 'unresolved Hebrew team name' / 'no DB "
            "match' warnings above and winner_odds.HE_TEAM_ALIASES.")

    log.warning(
        "winner gate: %d near match(es) still unpriced [%s], but the fresh bankerim "
        "cache (age %.1fh) does not list them yet -- bankerim has not posted the "
        "line; it auto-fills on the next post.", len(unpriced), names, age)
    return len(unpriced)


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    init_db()

    # Parse the worker-committed cache (live fallback locally), then write odds. Done
    # directly (NOT via daily_run.scrape_winner's @step) so the records flow into the
    # gate and a failure is never swallowed -- this routine is the authoritative,
    # loud-failing winner path.
    scraper = WinnerOddsScraper()
    records = scraper.fetch_records()
    with connect() as conn:
        scraper.upsert(records, conn)
    log.info("refresh_winner: %d Winner football matches parsed from cache/live",
             len(records))

    place_virtual_bets()   # six-player bets for matches now priced (idempotent)
    publish_web()          # refresh static-site feed + clear staleness canary

    unpriced = verify_winner_fresh(records)   # RAISES on hard failure -> RED job
    log.info("refresh_winner complete: Winner odds re-attempted, bets + feed written "
             "(%d near match(es) awaiting a not-yet-posted line)", unpriced)
    return 0


if __name__ == "__main__":
    sys.exit(main())
