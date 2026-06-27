"""Winner-odds parser + Hebrew-mapping tests.

The positive card is REAL markup captured from the bankerim Winner-Line page after
the 2026-06 aggregator redesign (Greece-Italy international friendly): the kickoff
datetime now lives in the `data-original-title` tooltip of `<span class="time">`
(the span text is the live status), the odds are nested in a `<span class="box-
colors">` wrapper, and a round-id tooltip sits on the same card. The negative
cards exercise the sport/market filters with minimal representative markup.
"""
import sqlite3
from datetime import UTC, date, datetime, timedelta

import pytest

from mondial.scrapers import winner_odds as wo
from mondial.scrapers.winner_odds import he_to_english, parse_cards

# Real card from bankerim.co.il (Winner-Line), whitespace preserved as served. The
# round-id span carries its OWN data-original-title ("תוכניה מס' ..."), so this also
# guards that the time regex anchors on class="time" and never grabs that tooltip.
_REAL_FOOTBALL = (
    '<div class="game is-father colorB  disableBtns closedEvent hasExtraData"  '
    'data-info-peroid="90 דק\'"  data-event-type="2X1" data-info-id="496880" '
    'data-info-roundid="2626010100" data-info-eventid="1338583" data-sportid="2" '
    'data-rankpower=\'["-1.40"]\'   > '
    '<span class="favorite"><i class="fa fa-star  "></i></span> '
    '<span class="round-id" data-toggle="tooltip" data-placement="top" title="" '
    'data-original-title="תוכניה מס\' 2626010100"><strong>2.</strong></span> '
    '<span class="status"><i class="bullet green"></i>'
    '<span data-toggle="tooltip" data-placement="top" title="" '
    'data-original-title="22:00 07.06.2026" class="time">ראשון 22:00</span></span> '
    '<span class="desc"  >יוון - איטליה</span> '
    '<span class="s-d" data-s="1" data-d="1">S,D</span> '
    '<span class="bet-home win"> <span class="box-colors"><span data-rate-change=\'0\'>1.80</span></span> </span> '
    '<span class="bet-x "> <span class="box-colors"><span data-rate-change=\'0\'>3.00</span></span> </span> '
    '<span class="bet-guest "> <span class="box-colors"><span data-rate-change=\'0\'>2.80</span></span> </span>'
)
# Basketball (sport != 2) -> filtered out.
_NON_FOOTBALL = (
    '<div class="game" data-event-type="2X1" data-sportid="3"> '
    '<span data-original-title="20:00 07.06.2026" class="time">x</span> '
    '<span class="desc">קבוצה א - קבוצה ב</span> '
    '<span class="bet-home ">1.50</span><span class="bet-x ">3.00</span><span class="bet-guest ">2.50</span></div>'
)
# Football but a derivative market (handicap suffix in desc) -> filtered out.
_HANDICAP = (
    '<div class="game" data-event-type="2X1" data-sportid="2"> '
    '<span data-original-title="20:00 07.06.2026" class="time">x</span> '
    '<span class="desc">יוון (1+) - איטליה</span> '
    '<span class="bet-home ">1.50</span><span class="bet-x ">3.00</span><span class="bet-guest ">2.50</span></div>'
)


def test_parse_extracts_real_football_1x2():
    recs = parse_cards(_REAL_FOOTBALL)
    assert len(recs) == 1
    r = recs[0]
    assert r["home_he"] == "יוון" and r["away_he"] == "איטליה"
    assert (r["odd_home"], r["odd_draw"], r["odd_away"]) == (1.80, 3.00, 2.80)
    # kickoff tooltip "22:00 07.06.2026" -> ISO; the round-id tooltip is ignored
    assert r["commence"] == "2026-06-07T22:00:00"


def test_parse_filters_non_football_and_derivatives():
    assert parse_cards(_NON_FOOTBALL) == []     # basketball dropped
    assert parse_cards(_HANDICAP) == []         # handicap market dropped
    # all three together -> only the clean football 1X2 survives
    assert len(parse_cards(_REAL_FOOTBALL + _NON_FOOTBALL + _HANDICAP)) == 1


def test_he_to_english_mapping_and_geresh():
    assert he_to_english("יוון") == "Greece"
    assert he_to_english("איטליה") == "Italy"
    assert he_to_english("צ'ילה") == "Chile"      # geresh normalised both sides
    assert he_to_english("נורבגיה") == "Norway"   # spelling variant
    assert he_to_english("קאפו ורדה") == "Cape Verde"  # 2026-06 site spelling
    assert he_to_english("ריאל מדריד ב'") is None  # a club -> not a national team


# --- cache reading (the Cloudflare-committed bankerim HTML; CI's source) --------

def test_fetch_cached_parses_committed_html(tmp_path, monkeypatch):
    """fetch_cached parses the worker-committed cache files with the same parser as
    a live fetch -- the line file alone yields the football 1X2 record."""
    monkeypatch.setattr(wo, "CACHE_DIR", tmp_path)
    (tmp_path / wo.CACHE_FILES["line"]).write_text(_REAL_FOOTBALL, encoding="utf-8")
    recs = wo.WinnerOddsScraper().fetch_cached()
    assert len(recs) == 1
    assert recs[0]["home_he"] == "יוון" and recs[0]["away_he"] == "איטליה"


def test_read_cache_meta_missing_and_present(tmp_path, monkeypatch):
    monkeypatch.setattr(wo, "CACHE_DIR", tmp_path)
    assert wo.read_cache_meta() is None
    (tmp_path / wo.CACHE_META).write_text('{"fetched_at": "2026-06-27T09:00:00+00:00"}')
    assert wo.read_cache_meta()["fetched_at"].startswith("2026-06-27")


# --- the hard verification gate (verify_winner_fresh) ---------------------------

def _make_gate_db(path):
    """Minimal teams/matches/odds DB with one NEAR upcoming WC fixture (France v
    Spain), unpriced. A logic fixture for the gate's branches (cf. test_calibration's
    toy-array unit tests) -- the full gate is also exercised end-to-end on the real DB."""
    conn = sqlite3.connect(path)
    conn.executescript(
        """CREATE TABLE teams(team_id INTEGER PRIMARY KEY, name TEXT);
           CREATE TABLE matches(match_id INTEGER PRIMARY KEY, tournament TEXT,
               status TEXT, date TEXT, home_id INT, away_id INT);
           CREATE TABLE odds_snapshots(match_id INT, bookmaker TEXT,
               odd_home REAL, odd_draw REAL, odd_away REAL);""")
    near = (date.today() + timedelta(days=1)).isoformat()
    conn.execute("INSERT INTO teams VALUES (1,'France'),(2,'Spain')")
    conn.execute("INSERT INTO matches VALUES (10,'WC','scheduled',?,1,2)", (near,))
    conn.commit()
    conn.close()


def _gate_env(tmp_path, monkeypatch, meta):
    from mondial.pipelines import refresh_winner as rw
    db = tmp_path / "gate.db"
    _make_gate_db(db)

    def opener():
        c = sqlite3.connect(db)
        c.row_factory = sqlite3.Row
        return c

    monkeypatch.setattr(rw, "connect", opener)
    monkeypatch.setattr(rw, "read_cache_meta", lambda: meta)
    return rw, db


def test_gate_raises_when_cache_absent(tmp_path, monkeypatch):
    """Near match unpriced + no cache committed -> hard RED (the worker fetch
    pipeline is down: the actual root cause). This is the never-silent guarantee."""
    rw, _ = _gate_env(tmp_path, monkeypatch, meta=None)
    with pytest.raises(RuntimeError, match="WINNER GATE FAILED"):
        rw.verify_winner_fresh([])


def test_gate_raises_when_fresh_cache_dropped_the_fixture(tmp_path, monkeypatch):
    """Near match unpriced + a FRESH cache that DOES list it (a card resolving to the
    two teams) -> hard RED (parser/resolver/alias bug, not 'line not posted')."""
    fresh = {"fetched_at": datetime.now(UTC).isoformat()}
    rw, _ = _gate_env(tmp_path, monkeypatch, meta=fresh)
    records = [{"home_he": "צרפת", "away_he": "ספרד", "commence": None}]  # -> France v Spain
    with pytest.raises(RuntimeError, match="NOT priced"):
        rw.verify_winner_fresh(records)


def test_gate_warns_when_line_not_posted_yet(tmp_path, monkeypatch):
    """Near match unpriced + fresh cache that does NOT list it -> WARN, no raise
    (bankerim has not posted the line yet)."""
    fresh = {"fetched_at": datetime.now(UTC).isoformat()}
    rw, _ = _gate_env(tmp_path, monkeypatch, meta=fresh)
    assert rw.verify_winner_fresh([]) == 1          # 1 unpriced, but soft


def test_gate_ok_when_match_is_priced(tmp_path, monkeypatch):
    """Priced near match -> nothing to verify, returns 0 (no raise)."""
    rw, db = _gate_env(tmp_path, monkeypatch, meta=None)
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO odds_snapshots VALUES (10,'winner',1.8,3.4,4.2)")
    conn.commit(); conn.close()
    assert rw.verify_winner_fresh([]) == 0
