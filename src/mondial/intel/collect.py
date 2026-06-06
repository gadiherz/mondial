"""Autonomous intel logger: refresh `team_intel` for upcoming-fixture teams.

For every team that appears in a scheduled match, research it once (using its
soonest upcoming match date as the as-of point) and upsert its intel vector.
Idempotent; safe to run on a schedule. Teams that fail to produce a vector are
left as-is (their last good intel stays; if none, the predictor treats them as
neutral and says so).
"""
from __future__ import annotations

import logging
import sqlite3
import time
from datetime import datetime, UTC

from mondial.config import settings
from mondial.intel import processor
from mondial.intel.llm_intel import fetch_team_intel

log = logging.getLogger("intel.collect")


def _upcoming_teams(
    conn: sqlite3.Connection, within_days: int | None = None,
) -> list[tuple[int, str, str]]:
    """(team_id, name, soonest_scheduled_date) for teams with a fixture.

    With `within_days` set, keep only teams whose next scheduled match falls in
    [today, today+within_days] -- the "teams playing this play-day" scope.
    """
    rows = conn.execute(
        """SELECT t.team_id, t.name, MIN(m.date) AS as_of
           FROM matches m
           JOIN teams t ON t.team_id IN (m.home_id, m.away_id)
           WHERE m.status='scheduled'
           GROUP BY t.team_id, t.name
           ORDER BY as_of, t.name"""
    ).fetchall()
    teams = [(r["team_id"], r["name"], r["as_of"]) for r in rows]
    if within_days is not None:
        from datetime import date, timedelta
        lo = date.today().isoformat()
        hi = (date.today() + timedelta(days=within_days)).isoformat()
        teams = [t for t in teams if lo <= t[2] <= hi]
    return teams


def collect_intel(
    conn: sqlite3.Connection, *, model: str | None = None,
    within_days: int | None = None, limit: int | None = None,
) -> int:
    """Refresh team_intel for upcoming-fixture teams. Returns rows written.

    `within_days` scopes to teams playing in the next N days (Routine A's
    play-day scope); `limit` caps the count (incremental within API budget).
    """
    teams = _upcoming_teams(conn, within_days=within_days)
    if limit is not None:
        teams = teams[:limit]
    log.info("intel: researching %d teams (model=%s, context<=%d chars/team)",
             len(teams), model or settings.intel_model,
             settings.intel_max_context_chars)

    processor.reset_budget_state()
    written = failed = 0
    deferred: list[str] = []
    for idx, (tid, name, as_of) in enumerate(teams):
        rr, rt = processor.remaining_requests(), processor.remaining_tokens()
        # --- Per-DAY guard: requests bucket low (or hard-exhausted) -> STOP and
        # defer the rest. This is the only condition that ends the run, because
        # the daily bucket does NOT refill within a run.
        if processor.is_exhausted() or (
                rr is not None and rr < settings.intel_min_remaining_requests):
            deferred = [t[1] for t in teams[idx:]]
            log.warning("intel: DAILY budget reached (remaining requests=%s); "
                        "STOPPING. %d teams deferred to next run (they keep their "
                        "prior vector / neutral, NOT zeroed).", rr, len(deferred))
            break
        # --- Per-MINUTE guard: tokens bucket low -> it refills, so just PACE
        # (wait), don't stop. Keeps the data flowing under the per-minute cap.
        if rt is not None and rt < settings.intel_min_remaining_tokens:
            log.info("intel: per-minute token bucket low (%s); pausing 5s to refill", rt)
            time.sleep(5)

        vec = fetch_team_intel(name, as_of, model=model)
        if vec is None:
            failed += 1
            continue
        comp = vec.composite()
        conn.execute(
            """INSERT INTO team_intel
               (team_id, as_of_date, form_outlook, availability, coach_stability,
                morale, motivation, confidence, composite, summary, model, fetched_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(team_id) DO UPDATE SET
                   as_of_date=excluded.as_of_date,
                   form_outlook=excluded.form_outlook,
                   availability=excluded.availability,
                   coach_stability=excluded.coach_stability,
                   morale=excluded.morale, motivation=excluded.motivation,
                   confidence=excluded.confidence, composite=excluded.composite,
                   summary=excluded.summary, model=excluded.model,
                   fetched_at=excluded.fetched_at""",
            (tid, as_of, vec.form_outlook, vec.availability, vec.coach_stability,
             vec.morale, vec.motivation, vec.confidence, comp, vec.summary,
             model or settings.intel_model, datetime.now(UTC).isoformat()),
        )
        written += 1
        log.info("  %-24s comp=%+.2f conf=%.2f  %s",
                 name, comp, vec.confidence, vec.summary[:80])

    if failed:
        log.warning("intel: %d teams produced no vector (left unchanged / neutral)",
                    failed)
    if deferred:
        shown = ", ".join(deferred[:12]) + (" ..." if len(deferred) > 12 else "")
        log.warning("intel: DEFERRED %d teams to a later run (budget): %s",
                    len(deferred), shown)
    log.info("intel: wrote %d team_intel rows (%d of %d teams covered)",
             written, written, len(teams))
    return written
