"""One-time repair: fix the WC-2026 knockout mess (phantom duplicates + wrong R32 slots).

Background (2026-06-29). Two faults combined once the knockout stage arrived:

  1. PHANTOM DUPLICATES. The Jurisoo results.csv lists the real-world WC-2026 knockout
     games at their real dates, and the old `refresh_international_results` INSERT-OR-
     IGNORE'd them as brand-new rows (stage/bracket_no NULL). Those collided with the
     bracket-generated fixtures (placeholder date 2026-06-30 + bracket_no) because
     UNIQUE(date, home_id, away_id) keys on date -> a SECOND row per pairing. They showed
     as bogus "group" games in July, stranded the real bracket row as 'scheduled', and
     tripped the winner-odds gate RED hourly. (Root cause fixed in pipelines/results.py:
     WC-2026 is now a score-only UPDATE, never an insert.)

  2. WRONG THIRD-PLACE SLOTS. `bracket._match_thirds` used a valid-but-arbitrary bipartite
     matching (THIRD_PLACE_OVERRIDE was empty), so three best-third teams landed in the
     wrong R32 slots vs the official draw (Germany-Bosnia/France-Paraguay/USA-Sweden
     instead of Germany-Paraguay/France-Sweden/USA-Bosnia). Those non-existent games were
     never priced and would stall the bracket. (Fixed by populating THIRD_PLACE_OVERRIDE.)

This script repairs the DB already in that state:
  A. Delete every knockout-window stray (WC, bracket_no IS NULL, date >= 2026-06-28; all
     group fixtures are on/before 06-27), transferring a real result onto its bracket row
     first if that row has none (oriented to the bracket row's home/away).
  B. With THIRD_PLACE_OVERRIDE now set, recompute the correct R32 and delete any still-
     'scheduled' bracket R32 row whose teams disagree, so generate_bracket re-creates it
     in the right slot.
  C. Rebuild downstream: resolve_knockout_winners -> settle -> generate_bracket ->
     predict_upcoming -> winner odds from cache -> place bets -> publish web feed.

Idempotent: a second run finds nothing to do. DRY-RUN by default; pass --apply to write.

Usage:
  PYTHONPATH=src python scripts/repair_knockout_dupes.py            # dry-run
  PYTHONPATH=src python scripts/repair_knockout_dupes.py --apply    # write
"""
from __future__ import annotations

import argparse
import logging
import sys

from mondial.data.db import connect

log = logging.getLogger("repair_knockout_dupes")

KNOCKOUT_WINDOW_START = "2026-06-28"   # every WC-2026 GROUP fixture is on/before 06-27
CHILD_TABLES = ("predictions", "match_breakdown", "match_features",
                "odds_snapshots", "bets")


def _strays(conn) -> list:
    """WC rows with bracket_no IS NULL in the knockout window -- CSV-inserted duplicates
    of the canonical bracket fixtures (no real group game is dated this late)."""
    return conn.execute(
        """SELECT m.match_id, m.date, m.status, m.home_id, m.away_id,
                  m.home_goals, m.away_goals, h.name AS home, a.name AS away
           FROM matches m
           JOIN teams h ON h.team_id = m.home_id
           JOIN teams a ON a.team_id = m.away_id
           WHERE m.tournament='WC' AND m.bracket_no IS NULL AND m.date >= ?
           ORDER BY m.date, m.match_id""",
        (KNOCKOUT_WINDOW_START,),
    ).fetchall()


def _bracket_row_for(conn, home_id: int, away_id: int):
    return conn.execute(
        """SELECT match_id, home_id, away_id, status, home_goals, away_goals
           FROM matches
           WHERE tournament='WC' AND bracket_no IS NOT NULL
             AND ((home_id=? AND away_id=?) OR (home_id=? AND away_id=?))""",
        (home_id, away_id, away_id, home_id),
    ).fetchone()


def _delete_match(conn, match_id: int, apply: bool) -> None:
    for t in CHILD_TABLES:           # FK enforcement: children before parent
        if apply:
            conn.execute(f"DELETE FROM {t} WHERE match_id=?", (match_id,))
    if apply:
        conn.execute("DELETE FROM matches WHERE match_id=?", (match_id,))


def repair(conn, apply: bool) -> int:
    name = {r["team_id"]: r["name"] for r in conn.execute("SELECT team_id, name FROM teams")}
    nid = {v: k for k, v in name.items()}

    # --- A) Strays ----------------------------------------------------------------
    strays = _strays(conn)
    transferred = 0
    log.info("A) %d knockout-window stray row(s) (bracket_no NULL, date>=%s):",
             len(strays), KNOCKOUT_WINDOW_START)
    for p in strays:
        br = _bracket_row_for(conn, p["home_id"], p["away_id"])
        score = (f"{p['home_goals']}-{p['away_goals']}"
                 if p["home_goals"] is not None else "no score")
        do_transfer = (br is not None and p["status"] == "final"
                       and p["home_goals"] is not None and br["home_goals"] is None)
        log.info("   id=%d %s %s v %s (%s, %s) -> bracket row %s%s",
                 p["match_id"], p["date"], p["home"], p["away"], score, p["status"],
                 br["match_id"] if br else "NONE",
                 " [transfer score]" if do_transfer else "")
        if do_transfer:
            hg, ag = ((p["home_goals"], p["away_goals"]) if br["home_id"] == p["home_id"]
                      else (p["away_goals"], p["home_goals"]))
        _delete_match(conn, p["match_id"], apply)    # delete stray first (frees the slot)
        if do_transfer:
            if apply:
                conn.execute(
                    """UPDATE matches SET home_goals=?, away_goals=?, status='final',
                              date=? WHERE match_id=? AND status='scheduled'""",
                    (hg, ag, p["date"], br["match_id"]))
            transferred += 1

    # --- B) Mis-slotted R32 bracket rows (vs the override-corrected draw) ----------
    # resolve_r32 needs all 72 group games final; safe here (group stage is complete).
    from mondial.model.bracket import resolve_r32
    correct = resolve_r32(conn)      # {bracket_no: (home_name, away_name)}
    fixed = 0
    log.info("B) checking %d R32 slots against the override-corrected draw:", len(correct))
    for bno, (home, away) in correct.items():
        row = conn.execute(
            "SELECT match_id, home_id, away_id, status FROM matches WHERE bracket_no=?",
            (bno,)).fetchone()
        if row is None:
            continue                 # missing -> generate_bracket will create it
        want = {nid.get(home), nid.get(away)}
        have = {row["home_id"], row["away_id"]}
        if want != have and row["status"] == "scheduled":
            log.info("   bno%d WRONG: have %s v %s, want %s v %s -> delete+regenerate",
                     bno, name.get(row["home_id"]), name.get(row["away_id"]), home, away)
            _delete_match(conn, row["match_id"], apply)
            fixed += 1

    if apply:
        log.info("APPLIED: %d stray(s) removed, %d result(s) transferred, %d R32 slot(s) "
                 "queued for regeneration.", len(strays), transferred, fixed)
    else:
        log.info("DRY-RUN: would remove %d stray(s), transfer %d result(s), fix %d R32 "
                 "slot(s). Re-run with --apply to write.", len(strays), transferred, fixed)
    return len(strays) + fixed


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry-run)")
    args = ap.parse_args()

    with connect() as conn:
        changed = repair(conn, args.apply)

    if args.apply and changed:
        # Rebuild downstream state on the now-canonical bracket rows.
        from mondial.pipelines.daily_run import (
            generate_bracket, place_virtual_bets, predict_upcoming, publish_web,
            scrape_winner, settle_virtual_bets)
        from mondial.pipelines.results import resolve_knockout_winners
        log.info("rebuilding: resolve winners -> settle -> bracket -> predict -> "
                 "winner odds -> place bets -> publish")
        resolve_knockout_winners()
        settle_virtual_bets()
        generate_bracket()       # re-creates the corrected R32 slots
        predict_upcoming()       # predictions for the corrected/new fixtures (local)
        scrape_winner()          # price them from the committed bankerim cache
        place_virtual_bets()
        publish_web()
        log.info("repair complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
