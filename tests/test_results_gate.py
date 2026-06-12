"""The event-driven Odds API gate: fire only while a finished WC fixture awaits
its score, so 10-min polling costs a few credits per match, not one per tick."""
import sqlite3
from datetime import UTC, datetime, timedelta

from mondial.pipelines.results import (
    FINISH_AFTER_MIN, GIVEUP_AFTER_MIN, _odds_scan_warranted,
)

NOW = datetime(2026, 6, 12, 12, 0, tzinfo=UTC)


def _db():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("""CREATE TABLE matches (match_id INTEGER PRIMARY KEY, tournament TEXT,
                 status TEXT, date TEXT, kickoff_utc TEXT)""")
    return c


def _add(c, mid, *, status="scheduled", ko_min_ago=None, date=None):
    ko = (NOW - timedelta(minutes=ko_min_ago)).isoformat().replace("+00:00", "Z") \
        if ko_min_ago is not None else None
    d = date or NOW.date().isoformat()
    c.execute("INSERT INTO matches VALUES (?,?,?,?,?)", (mid, "WC", status, d, ko))


def test_fires_just_after_finish():
    c = _db(); _add(c, 1, ko_min_ago=FINISH_AFTER_MIN + 5)
    assert _odds_scan_warranted(c, NOW) is True


def test_silent_while_match_in_play():
    c = _db(); _add(c, 1, ko_min_ago=60)  # 60' in, not finished
    assert _odds_scan_warranted(c, NOW) is False


def test_gives_up_after_window():
    c = _db(); _add(c, 1, ko_min_ago=GIVEUP_AFTER_MIN + 30)
    assert _odds_scan_warranted(c, NOW) is False


def test_final_match_ignored():
    c = _db(); _add(c, 1, status="final", ko_min_ago=FINISH_AFTER_MIN + 5)
    assert _odds_scan_warranted(c, NOW) is False


def test_unknown_kickoff_falls_back_to_date_window():
    c = _db(); _add(c, 1, ko_min_ago=None, date=NOW.date().isoformat())
    assert _odds_scan_warranted(c, NOW) is True


def test_unknown_kickoff_old_date_skipped():
    c = _db(); _add(c, 1, ko_min_ago=None, date="2026-06-01")
    assert _odds_scan_warranted(c, NOW) is False
