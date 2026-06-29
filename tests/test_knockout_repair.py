"""Regression tests for the 2026-06-29 knockout-duplicate fix.

Two faults, both covered here:
  * Part 1 -- WC-2026 results are a score-only UPDATE of the existing fixture, resolved
    by unordered team pair, date-floored to 2026 (never inserts a duplicate / never lands
    on a historical WC edition). See pipelines.results._resolve_wc2026_fixture / _is_wc2026.
  * Part 4 -- the official third-place slot assignment for the real 2026 qualifying
    combination is registered in bracket.THIRD_PLACE_OVERRIDE.
"""
from mondial.data.db import connect, init_db
from mondial.model import bracket as B
from mondial.pipelines.results import _is_wc2026, _resolve_wc2026_fixture


# --- Part 1: WC-2026 record classification + pair resolution -------------------
def test_is_wc2026_only_2026_world_cup():
    assert _is_wc2026({"tournament": "FIFA World Cup", "date": "2026-06-30"}) is True
    # historical WC editions share the tournament string but predate the floor
    assert _is_wc2026({"tournament": "FIFA World Cup", "date": "2022-12-18"}) is False
    # other tournaments go through the generic insert path
    assert _is_wc2026({"tournament": "Friendly", "date": "2026-06-05"}) is False


def _seed(path):
    init_db(path)
    with connect(path) as conn:
        conn.execute("INSERT INTO teams (team_id,name,confederation) VALUES (1,'Germany','X')")
        conn.execute("INSERT INTO teams (team_id,name,confederation) VALUES (2,'Paraguay','X')")
        # A 2026 knockout bracket row (placeholder date, scheduled) for Germany v Paraguay.
        conn.execute("""INSERT INTO matches (match_id,date,home_id,away_id,tournament,
                        neutral,status,stage,bracket_no)
                        VALUES (100,'2026-06-30',1,2,'WC',1,'scheduled','R32',74)""")
        # A historical WC meeting of the same pair (must NOT be chosen).
        conn.execute("""INSERT INTO matches (match_id,date,home_id,away_id,tournament,
                        home_goals,away_goals,neutral,status)
                        VALUES (101,'2018-06-27',1,2,'WC',1,0,1,'final')""")
    return path


def test_resolve_wc2026_fixture_picks_2026_scheduled_bracket_row(tmp_path):
    db = _seed(tmp_path / "k.db")
    with connect(db) as conn:
        m = _resolve_wc2026_fixture(conn, 1, 2)         # home/away order
        assert m["match_id"] == 100
        m_rev = _resolve_wc2026_fixture(conn, 2, 1)     # unordered pair
        assert m_rev["match_id"] == 100


def test_resolve_wc2026_fixture_none_for_unknown_pair(tmp_path):
    db = _seed(tmp_path / "k.db")
    with connect(db) as conn:
        assert _resolve_wc2026_fixture(conn, 1, 999) is None


# --- Part 4: official 2026 third-place override --------------------------------
def test_third_place_override_matches_real_draw():
    qual = frozenset("BDEFIJKL")
    assert qual in B.THIRD_PLACE_OVERRIDE
    assignment = B._match_thirds(qual)
    assert assignment == {"B": 81, "D": 74, "E": 79, "F": 77,
                          "I": 82, "J": 85, "K": 80, "L": 87}
    # every assignment respects its slot's allowed-group set
    assert all(g in B.THIRDS_SLOTS[m] for g, m in assignment.items())
