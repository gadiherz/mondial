"""Routine B — Results ingestion (immutable, write-once).

Pulls completed scores from the Odds API `/scores` endpoint (reuses the existing
ODDS_API_KEY), flips matching scheduled fixtures to status='final' with their
goals, and re-runs the leakage-free Glicko/momentum backfill so the ratings (and
thus the next prediction round) reflect the new results.

Separate from Routine A by design: a result is captured exactly once and never
changes, the source is a clean API, and the job is light — so it can run on a
frequent cron during match windows without touching the heavy intel/odds routine.
"""
from __future__ import annotations

import logging
import sys
from datetime import UTC, datetime, timedelta

from mondial.data.db import connect, init_db
from mondial.pipelines.daily_run import (
    generate_bracket,
    publish_web,
    settle_virtual_bets,
    update_ratings_for_completed,
)
from mondial.scrapers.historical_results import HistoricalResultsScraper, _is_nan
from mondial.scrapers.odds_api import OddsAPIScraper, _norm_index, resolve_match, resolve_team_id

log = logging.getLogger("results")

# Re-fetch a small window of already-known dates too, so scores posted late
# (the CSV updates a fixture from scheduled -> played a day or two after) are
# still picked up on the next run.
REFRESH_OVERLAP_DAYS = 7


def refresh_international_results() -> int:
    """Comprehensive results refresh from the Jurisoo CSV (incl. prep friendlies).

    The Odds API `/scores` path below only sees events a book priced; national-team
    friendlies in the June prep window mostly aren't there. The free, key-less
    Jurisoo dataset (github.com/martj42/international_results) carries every
    international result, so it is the primary results source. These prep
    friendlies feed the momentum features (resid_ewma / gd_ewma) that are the
    model's black-swan signal, so ingesting them before the next predict round
    matters.

    Returns the number of matches newly marked 'final' (new inserts +
    scheduled->final promotions). The base scraper.upsert is INSERT-OR-IGNORE and
    cannot promote an existing scheduled fixture, so we run an explicit promotion
    pass for fixtures whose score has since appeared.
    """
    with connect() as conn:
        last = conn.execute(
            "SELECT MAX(date) AS d FROM matches WHERE status='final'"
        ).fetchone()["d"]
    since = datetime.fromisoformat(last or "2014-01-01") - timedelta(days=REFRESH_OVERLAP_DAYS)

    records = HistoricalResultsScraper().fetch(since=since)
    scraper = HistoricalResultsScraper()
    with connect() as conn:
        before = conn.execute(
            "SELECT COUNT(*) AS n FROM matches WHERE status='final'"
        ).fetchone()["n"]
        # Creates any new teams, inserts brand-new played matches as 'final' and
        # brand-new fixtures as 'scheduled' (existing rows untouched).
        scraper.upsert(records, conn)
        # Promotion pass: finalize any still-'scheduled' fixture whose score is
        # now present in the fresh records (the INSERT-OR-IGNORE gap).
        name_to_id = {r["name"]: r["team_id"]
                      for r in conn.execute("SELECT team_id, name FROM teams")}
        for rec in records:
            if _is_nan(rec["home_score"]) or _is_nan(rec["away_score"]):
                continue
            h = name_to_id.get(rec["home_team"]); a = name_to_id.get(rec["away_team"])
            if h is None or a is None:
                continue
            conn.execute(
                """UPDATE matches SET home_goals=?, away_goals=?, status='final'
                   WHERE date=? AND home_id=? AND away_id=? AND status='scheduled'""",
                (int(rec["home_score"]), int(rec["away_score"]), rec["date"], h, a),
            )
        after = conn.execute(
            "SELECT COUNT(*) AS n FROM matches WHERE status='final'"
        ).fetchone()["n"]
    new_finals = after - before
    log.info("refresh: Jurisoo CSV since %s -> %d newly-final matches",
             since.date().isoformat(), new_finals)
    return new_finals


def _odds_scan_warranted(conn) -> bool:
    """True iff a WC fixture dated today or yesterday (UTC) is still unfinalized.

    Gates the paid Odds API /scores call so the HOURLY cron only spends quota when
    a match could plausibly have just finished. The DB stores match dates without a
    clock time, so we use a 1-day window around 'today' to cover timezone edges.
    On empty hours (no match day, or the day's slate already final) we skip the API
    entirely and rely on the free Jurisoo CSV refresh.
    """
    today = datetime.now(UTC).date()
    yday = today - timedelta(days=1)
    row = conn.execute(
        """SELECT 1 FROM matches
           WHERE tournament='WC' AND status='scheduled' AND date IN (?, ?)
           LIMIT 1""",
        (yday.isoformat(), today.isoformat()),
    ).fetchone()
    return row is not None


def ingest_results() -> int:
    """Finalize scheduled matches that have completed scores. Returns count."""
    with connect() as conn:
        if not _odds_scan_warranted(conn):
            log.info("results: no unfinalized WC fixture dated today/yesterday; "
                     "skipping paid Odds API /scores call (quota saver).")
            return 0
    events = OddsAPIScraper().fetch_scores()
    finalized = 0
    with connect() as conn:
        idx = _norm_index(conn)
        for ev in events:
            if not ev.get("completed") or not ev.get("scores"):
                continue
            h_id = resolve_team_id(ev.get("home_team") or "", idx)
            a_id = resolve_team_id(ev.get("away_team") or "", idx)
            if h_id is None or a_id is None:
                continue
            commence = datetime.fromisoformat(ev["commence_time"].replace("Z", "+00:00"))
            m = resolve_match(conn, h_id, a_id, commence)
            if m is None or m["status"] == "final":
                continue
            goals: dict[int, int] = {}
            for s in ev["scores"]:
                tid = resolve_team_id(s.get("name") or "", idx)
                try:
                    goals[tid] = int(s.get("score"))
                except (TypeError, ValueError):
                    pass
            hg, ag = goals.get(m["home_id"]), goals.get(m["away_id"])
            if hg is None or ag is None:
                continue
            conn.execute(
                """UPDATE matches SET home_goals=?, away_goals=?, status='final'
                   WHERE match_id=? AND status='scheduled'""",
                (hg, ag, m["match_id"]),
            )
            finalized += 1
            log.info("results: finalized %s %d-%d %s",
                     ev.get("home_team"), hg, ag, ev.get("away_team"))
    log.info("results: %d newly-finalized matches", finalized)
    return finalized


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    init_db()

    # Primary, comprehensive, key-less source (catches prep friendlies).
    n_csv = refresh_international_results()

    # Supplementary low-latency source during WC match windows; failure-isolated
    # so a missing/rotated ODDS_API_KEY never blocks the CSV-driven refresh.
    n_api = 0
    try:
        n_api = ingest_results()
    except Exception as e:  # noqa: BLE001
        log.warning("results: Odds API /scores path skipped (%s)", e)

    if n_csv + n_api > 0:
        update_ratings_for_completed()  # refresh Glicko/momentum from new finals
    else:
        log.info("results: nothing new; ratings unchanged.")

    # Always settle: a bet placed by Routine A may have a match that finalized in
    # an earlier results run. Cheap + idempotent (no-op when nothing is open/final).
    settle_virtual_bets()
    # Generate knockout fixtures once results determine them (no-op pre-knockout).
    generate_bracket()
    publish_web()   # refresh the static-site feed with new results/standings
    return 0


if __name__ == "__main__":
    sys.exit(main())
