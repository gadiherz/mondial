"""WC-2026 knockout bracket: template integrity + standings/qualification rules.

Group results are constructed to exercise the deterministic ranking/seeding rules
(analogous to test_dixon_coles's toy matches); they are not model-validation data.
"""
import pytest

from mondial.data.db import connect, init_db
from mondial.model import bracket as B
from mondial.model.bracket import (
    GROUPS,
    KO_TREE,
    R32_SLOTS,
    THIRDS_SLOTS,
    TeamRecord,
    _match_thirds,
    best_thirds,
    generate_knockout,
    rank_group,
    resolve_r32,
    validate_groups,
)


# --- template integrity (pure constants) -----------------------------------
def test_template_shapes():
    assert len(GROUPS) == 12 and all(len(t) == 4 for t in GROUPS.values())
    assert len({t for ts in GROUPS.values() for t in ts}) == 48
    assert len(R32_SLOTS) == 16
    assert len(THIRDS_SLOTS) == 8
    assert set(KO_TREE) == set(range(89, 105))   # R16..final incl. 3rd place


def test_every_group_seeded_once_in_r32():
    # Each group's winner (1X) and runner-up (2X) appears exactly once across R32.
    ones, twos = [], []
    for sa, sb in R32_SLOTS.values():
        for kind, ref in (sa, sb):
            if kind == "1":
                ones.append(ref)
            elif kind == "2":
                twos.append(ref)
    assert sorted(ones) == list("ABCDEFGHIJKL")
    assert sorted(twos) == list("ABCDEFGHIJKL")


# --- ranking / tiebreakers --------------------------------------------------
def test_rank_group_head_to_head_breaks_tie():
    # X and Y identical on points/GD/GF; X won their head-to-head -> X ranks higher.
    x, y = TeamRecord("X", 3, 2, 0, 1, 4, 2), TeamRecord("Y", 3, 2, 0, 1, 4, 2)
    x.opp_goals["Y"] = (1, 0)
    y.opp_goals["X"] = (0, 1)
    ranked = rank_group([y, x])
    assert ranked[0].team == "X"


def test_best_thirds_orders_by_points_then_gd():
    # 12 thirds with descending points/GD -> top 8 selected, best first.
    standings = {}
    for i, g in enumerate("ABCDEFGHIJKL"):
        r = TeamRecord(f"T{g}", 3, 1, 0, 2, 3 - i, 5)  # gf descends, so GD descends
        r.w, r.d = (1, 0)
        standings[g] = [TeamRecord("w"), TeamRecord("ru"), r]
    top = best_thirds(standings)
    assert len(top) == 8
    assert [g for g, _ in top] == list("ABCDEFGH")


# --- thirds allocation ------------------------------------------------------
def test_match_thirds_is_valid_bijection():
    qual = frozenset("EFGHIJKL")
    assign = _match_thirds(qual)              # {group: match_no}
    assert set(assign) == qual                # every qualifying group placed
    assert len(set(assign.values())) == 8     # to distinct slots
    for g, m in assign.items():
        assert g in THIRDS_SLOTS[m]           # respects the slot's allowed set


def test_match_thirds_override_wins(monkeypatch):
    qual = frozenset("ABCDEFGH")
    forced = {g: m for g, m in zip("ABCDEFGH", sorted(THIRDS_SLOTS))}
    monkeypatch.setitem(B.THIRD_PLACE_OVERRIDE, qual, forced)
    assert _match_thirds(qual) == forced


# --- full group stage -> R32 (integration on a temp DB) ---------------------
def _seed_group_stage(path):
    init_db(path)
    with connect(path) as conn:
        ids = {}
        for i, t in enumerate({t for ts in GROUPS.values() for t in ts}, 1):
            conn.execute("INSERT INTO teams (team_id, name, confederation) "
                         "VALUES (?, ?, 'X')", (i, t))
            ids[t] = i
        # Per group: t0 wins all, t1 beats t2/t3, t2 beats t3 by a group-specific
        # margin so the 12 thirds rank A>B>...>L (top 8 = A..H).
        for gi, (g, (t0, t1, t2, t3)) in enumerate(GROUPS.items()):
            m = 12 - gi
            games = [(t0, t1, 1, 0), (t0, t2, 1, 0), (t0, t3, 1, 0),
                     (t1, t2, 1, 0), (t1, t3, 1, 0), (t2, t3, m, 0)]
            for k, (h, a, hg, ag) in enumerate(games):
                conn.execute(
                    """INSERT INTO matches (date, home_id, away_id, tournament,
                       home_goals, away_goals, neutral, status)
                       VALUES (?, ?, ?, 'WC', ?, ?, 0, 'final')""",
                    (f"2026-06-{15 + k}", ids[h], ids[a], hg, ag))
    return path


def test_full_group_stage_generates_r32(tmp_path):
    db = _seed_group_stage(tmp_path / "wc.db")
    with connect(db) as conn:
        validate_groups(conn)                       # GROUPS match the seeded cliques
        r32 = resolve_r32(conn)
        assert len(r32) == 16
        # Fixed slots resolve to the right group winners/runners-up.
        assert r32[73] == (GROUPS["A"][1], GROUPS["B"][1])   # 2A vs 2B
        assert r32[75] == (GROUPS["F"][0], GROUPS["C"][1])   # 1F vs 2C
        assert r32[88] == (GROUPS["D"][1], GROUPS["G"][1])   # 2D vs 2G
        # A thirds slot: 1E vs a third from one of A/B/C/D/F (and in the top-8 set).
        home74, away74 = r32[74]
        assert home74 == GROUPS["E"][0]
        thirds_by_group = {g: GROUPS[g][2] for g in "ABCDFGH"}
        assert away74 in {thirds_by_group[g] for g in "ABCDF"}

        assert generate_knockout(conn) == 16        # writes the 16 R32 fixtures
        assert generate_knockout(conn) == 0         # idempotent
        n = conn.execute("SELECT COUNT(*) n FROM matches WHERE stage='R32'"
                         ).fetchone()["n"]
        assert n == 16


def test_generate_knockout_noop_before_group_stage(tmp_path):
    # Only a couple of group games final -> no R32 yet.
    init_db(tmp_path / "e.db")
    with connect(tmp_path / "e.db") as conn:
        assert generate_knockout(conn) == 0


def test_winner_loser_advances_via_winner_id(tmp_path):
    """A knockout level at 90'/ET (penalties) cannot advance from goals -> it advances
    by `winner_id` instead; a decisive score still resolves without one."""
    init_db(tmp_path / "ko.db")
    with connect(tmp_path / "ko.db") as conn:
        conn.execute("INSERT INTO teams (team_id, name, confederation) VALUES "
                     "(1,'France','UEFA'),(2,'Italy','UEFA')")
        conn.execute("INSERT INTO matches (match_id, date, home_id, away_id, tournament, "
                     "neutral, status, stage, bracket_no, home_goals, away_goals) VALUES "
                     "(200,'2026-06-30',1,2,'WC',1,'final','R32',73,1,1)")     # level 1-1
        # Level + no winner_id -> deferred (the old stuck case).
        assert B._winner_loser(conn, 73) == (None, None)
        # Penalty winner recorded -> advances Italy (the loser is France).
        conn.execute("UPDATE matches SET winner_id=2 WHERE match_id=200")
        assert B._winner_loser(conn, 73) == ("Italy", "France")
        # Decisive goals still resolve with winner_id cleared.
        conn.execute("UPDATE matches SET winner_id=NULL, home_goals=2, away_goals=0 "
                     "WHERE match_id=200")
        assert B._winner_loser(conn, 73) == ("France", "Italy")
