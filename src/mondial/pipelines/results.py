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
from mondial.scrapers.odds_api import (
    OddsAPIScraper, _norm_index, record_kickoff, resolve_match, resolve_team_id,
)

log = logging.getLogger("results")

# Re-fetch a small window of already-known dates too, so scores posted late
# (the CSV updates a fixture from scheduled -> played a day or two after) are
# still picked up on the next run.
REFRESH_OVERLAP_DAYS = 7

# The entire WC-2026 schedule (group + knockout) already lives in the DB: the 72
# group fixtures are seeded and the knockout fixtures are created by
# model.bracket.generate_knockout (with a placeholder date + bracket_no). So the
# Jurisoo CSV must only ever supply SCORES for WC-2026 -- it must NEVER insert a
# WC-2026 fixture row. The blind INSERT-OR-IGNORE upsert keys on
# UNIQUE(date, home_id, away_id); the bracket's placeholder date differs from the
# real knockout date, so a blind insert created a DUPLICATE (stage/bracket_no NULL)
# of every knockout game, which (a) littered the site with phantom "group" games,
# (b) stranded the real bracket row as 'scheduled' so the bracket stalled, and
# (c) tripped the winner-odds freshness gate RED (the phantom was unpriced while
# its team-pair WAS in the cache). We therefore route WC-2026 results through a
# pair-based, score-only UPDATE of the existing fixture and keep them out of the
# generic insert path. Date-floored to 2026 so a pair never lands on the historical
# 2014/2018/2022 WC editions that share tournament='WC'.
WC2026_FLOOR = "2026-01-01"


def _is_wc2026(rec: dict) -> bool:
    return rec.get("tournament") == "FIFA World Cup" and str(rec["date"]) >= WC2026_FLOOR


def _resolve_wc2026_fixture(conn, h_id: int, a_id: int):
    """Existing WC-2026 fixture for an unordered team pair (or None).

    Prefers a still-'scheduled' row, then the one nearest today -- the same shape as
    scrapers.winner_odds._resolve_pair, but date-floored to 2026 so it can't resolve
    onto a historical WC edition. A national-team pair meets at most once in a
    tournament window, so the unordered pair is a safe key even though the bracket
    row carries a placeholder date.
    """
    rows = conn.execute(
        """SELECT match_id, home_id, away_id, date, status FROM matches
           WHERE tournament='WC' AND date >= ?
             AND ((home_id=? AND away_id=?) OR (home_id=? AND away_id=?))""",
        (WC2026_FLOOR, h_id, a_id, a_id, h_id),
    ).fetchall()
    if not rows:
        return None
    today = datetime.now(UTC).date()
    return min(
        rows,
        key=lambda r: (r["status"] != "scheduled",
                       abs((datetime.fromisoformat(r["date"]).date() - today).days)),
    )


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
    # WC-2026 results are score-only UPDATES of the already-present fixtures (see
    # WC2026_FLOOR note); everything else (training data / friendlies / other
    # tournaments) goes through the generic insert+promote path unchanged.
    wc_records = [r for r in records if _is_wc2026(r)]
    other_records = [r for r in records if not _is_wc2026(r)]
    scraper = HistoricalResultsScraper()
    with connect() as conn:
        before = conn.execute(
            "SELECT COUNT(*) AS n FROM matches WHERE status='final'"
        ).fetchone()["n"]
        # Creates any new teams, inserts brand-new played matches as 'final' and
        # brand-new fixtures as 'scheduled' (existing rows untouched). NOT WC-2026.
        scraper.upsert(other_records, conn)
        name_to_id = {r["name"]: r["team_id"]
                      for r in conn.execute("SELECT team_id, name FROM teams")}
        # Promotion pass: finalize any still-'scheduled' NON-WC-2026 fixture whose
        # score is now present in the fresh records (the INSERT-OR-IGNORE gap).
        for rec in other_records:
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
        # WC-2026 pass: pair-match the existing fixture (group OR knockout bracket
        # row) and write the score onto it -- NEVER insert a new row. Finalizes a
        # still-'scheduled' fixture (the bracket row keeps a placeholder date until
        # its real result lands here, then we set the real date too), orienting goals
        # to OUR DB home/away. This is what advances the knockout bracket and what
        # keeps the phantom duplicates from ever being created.
        wc_updated = 0
        for rec in wc_records:
            if _is_nan(rec["home_score"]) or _is_nan(rec["away_score"]):
                continue
            h = name_to_id.get(rec["home_team"]); a = name_to_id.get(rec["away_team"])
            if h is None or a is None:
                continue
            m = _resolve_wc2026_fixture(conn, h, a)
            if m is None:
                log.warning("results: WC-2026 CSV record %s v %s (%s) matches no "
                            "existing fixture -- skipped (NOT inserting a duplicate).",
                            rec["home_team"], rec["away_team"], rec["date"])
                continue
            csv_h, csv_a = int(rec["home_score"]), int(rec["away_score"])
            hg, ag = (csv_h, csv_a) if m["home_id"] == h else (csv_a, csv_h)
            cur = conn.execute(
                """UPDATE matches SET home_goals=?, away_goals=?, status='final', date=?
                   WHERE match_id=? AND status='scheduled'""",
                (hg, ag, rec["date"], m["match_id"]),
            )
            wc_updated += cur.rowcount
        if wc_updated:
            log.info("results: WC-2026 score-only pass finalized %d existing "
                     "fixture(s) (no duplicates inserted)", wc_updated)
        after = conn.execute(
            "SELECT COUNT(*) AS n FROM matches WHERE status='final'"
        ).fetchone()["n"]
    new_finals = after - before
    log.info("refresh: Jurisoo CSV since %s -> %d newly-final matches",
             since.date().isoformat(), new_finals)
    return new_finals


# A match should be over ~FINISH_AFTER_MIN past kickoff (90' + half-time +
# stoppage, ~5 min margin). We then "keep insisting" every cron tick until the
# score lands or GIVEUP_AFTER_MIN passes (covers extra time / penalties / late
# postings; beyond that the daily Jurisoo CSV is the backstop).
FINISH_AFTER_MIN = 110
GIVEUP_AFTER_MIN = 8 * 60


def _odds_scan_warranted(conn, now: datetime | None = None) -> bool:
    """True iff some unfinalized WC fixture is in its post-kickoff results window.

    Event-driven gate for the paid Odds API /scores call: it fires only from
    ~FINISH_AFTER_MIN after a fixture's real kickoff (captured in kickoff_utc from
    the odds feed) until the score is in or GIVEUP_AFTER_MIN elapses. So on a
    match day the scanner polls every cron tick right after each game ends and
    stops once it's final -- a handful of calls per match, never all day. Fixtures
    whose kickoff time isn't known yet fall back to a date window so we never miss
    one. Outside any window we skip the API and rely on the free CSV refresh.
    """
    now = now or datetime.now(UTC)
    rows = conn.execute(
        """SELECT kickoff_utc, date FROM matches
           WHERE tournament='WC' AND status='scheduled'
             AND date BETWEEN ? AND ?""",
        ((now.date() - timedelta(days=2)).isoformat(),
         (now.date() + timedelta(days=1)).isoformat()),
    ).fetchall()
    for r in rows:
        ko = r["kickoff_utc"]
        if ko:
            kt = datetime.fromisoformat(ko.replace("Z", "+00:00"))
            mins = (now - kt).total_seconds() / 60.0
            if FINISH_AFTER_MIN <= mins <= GIVEUP_AFTER_MIN:
                return True
        else:
            # Kickoff time unknown (odds not scraped yet): coarse fallback so a
            # result is never missed -- poll if the fixture is dated today/yesterday.
            if r["date"] in (now.date().isoformat(),
                             (now.date() - timedelta(days=1)).isoformat()):
                return True
    return False


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
            if m is None:
                continue
            record_kickoff(conn, m["match_id"], commence)  # keep kickoff times fresh
            if m["status"] == "final":
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


def resolve_knockout_winners() -> int:
    """Fill matches.winner_id for FINAL knockout ties -- the team that ADVANCES.

    Decisive on goals (90'+ET) -> the higher score. Level on goals (a penalty shootout,
    since results.csv excludes penalties) -> the martj42 shootouts.csv winner, matched by
    unordered team pair (our knockout fixtures carry a placeholder date, so pair-not-date
    is the key). A level tie with no shootout record yet is DEFERRED (loud log) and
    retried next run. This drives bracket advancement ONLY (model/bracket); the 1X2 bet
    settles separately on the 90' result (eval/betting + matches.reg_result), so a tie
    that goes to penalties still settles as a Draw.
    """
    with connect() as conn:
        rows = conn.execute(
            """SELECT m.match_id, m.home_id, m.away_id, m.home_goals, m.away_goals,
                      th.name hn, ta.name an
               FROM matches m JOIN teams th ON th.team_id=m.home_id
               JOIN teams ta ON ta.team_id=m.away_id
               WHERE m.tournament='WC' AND m.bracket_no IS NOT NULL
                 AND m.status='final' AND m.winner_id IS NULL
                 AND m.home_goals IS NOT NULL AND m.away_goals IS NOT NULL"""
        ).fetchall()
        if not rows:
            return 0
        # Only pull the shootouts CSV when a level tie actually needs it.
        shootouts: dict = {}
        if any(r["home_goals"] == r["away_goals"] for r in rows):
            try:
                shootouts = HistoricalResultsScraper().fetch_shootouts(
                    since=datetime(2026, 1, 1))
            except Exception as e:  # noqa: BLE001
                log.warning("results: shootouts.csv fetch failed (%s); level ties "
                            "deferred to next run", e)
        name_to_id = {r["name"]: r["team_id"]
                      for r in conn.execute("SELECT team_id, name FROM teams")}
        filled = 0
        for r in rows:
            hg, ag = r["home_goals"], r["away_goals"]
            if hg != ag:
                wid = r["home_id"] if hg > ag else r["away_id"]
            else:
                wname = shootouts.get(frozenset((r["hn"], r["an"])))
                wid = name_to_id.get(wname) if wname else None
                if wid is None:
                    log.warning("results: knockout %s v %s level at %d-%d but no penalty "
                                "winner found yet -- DEFERRED (bracket waits). Correct "
                                "manually via scripts/set_knockout_result.py if it stalls.",
                                r["hn"], r["an"], hg, ag)
                    continue
            conn.execute("UPDATE matches SET winner_id=? WHERE match_id=?",
                         (wid, r["match_id"]))
            filled += 1
            log.info("results: %s advances (winner_id set, match_id=%d)",
                     r["hn"] if wid == r["home_id"] else r["an"], r["match_id"])
    if filled:
        log.info("results: resolved %d knockout advancing team(s)", filled)
    return filled


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

    # Resolve who ADVANCES for any finished knockout tie (goals, else penalty winner)
    # BEFORE settling/bracketing, so the bracket can advance past a draw and the loud
    # "deferred" warning fires while there is still a chance to correct it.
    try:
        resolve_knockout_winners()
    except Exception as e:  # noqa: BLE001
        log.warning("results: knockout winner resolution skipped (%s)", e)
    # Always settle: a bet placed by Routine A may have a match that finalized in
    # an earlier results run. Cheap + idempotent (no-op when nothing is open/final).
    settle_virtual_bets()
    # Generate knockout fixtures once results determine them (no-op pre-knockout).
    generate_bracket()
    publish_web()   # refresh the static-site feed with new results/standings
    return 0


if __name__ == "__main__":
    sys.exit(main())
