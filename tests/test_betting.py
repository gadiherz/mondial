"""Live six-player betting sim: match-day placement -> settle -> leaderboard."""
from mondial.data.db import connect, init_db
from mondial.eval.betting import leaderboard, place_bets, settle_bets
from mondial.eval.simulator import STAKE_NIS

MATCH_DAY = "2026-06-11"


def _seed(path):
    """A minimal real DB: two teams, one scheduled+priced+predicted match on MATCH_DAY."""
    init_db(path)
    with connect(path) as conn:
        conn.execute("INSERT INTO teams (team_id, name, confederation) VALUES "
                     "(1,'Mexico','CONCACAF'),(2,'South Africa','CAF')")
        conn.execute("INSERT INTO matches (match_id, date, home_id, away_id, "
                     "tournament, neutral, status) VALUES "
                     "(100,?,1,2,'WC',0,'scheduled')", (MATCH_DAY,))
        conn.execute("INSERT INTO predictions (match_id, p_home, p_draw, p_away, "
                     "predicted_at) VALUES (100,0.65,0.22,0.13,'t')")
        conn.execute("INSERT INTO odds_snapshots (match_id, bookmaker, ts, "
                     "odd_home, odd_draw, odd_away) VALUES "
                     "(100,'winner','t',1.40,4.00,7.00)")
    return path


def test_match_day_guard_then_six_bets_idempotent(tmp_path):
    db = _seed(tmp_path / "b.db")
    with connect(db) as conn:
        # GUARD: before the match day, nothing is bet (no pre-betting future matches).
        assert place_bets(conn, as_of="2026-06-10") == 0
        assert conn.execute("SELECT COUNT(*) c FROM bets").fetchone()["c"] == 0
        # On the match day: one bet per player.
        assert place_bets(conn, as_of=MATCH_DAY) == 6
        assert place_bets(conn, as_of=MATCH_DAY) == 0          # idempotent
        rows = conn.execute("SELECT player_name, pick, stake, odd, settled "
                            "FROM bets").fetchall()
    assert {r["player_name"] for r in rows} == {
        "model_full", "model_fundamental", "safe", "tide", "risk", "monkey"}
    for r in rows:
        assert r["stake"] == STAKE_NIS and r["settled"] == 0 and r["odd"] > 0
    pick = {r["player_name"]: r["pick"] for r in rows}
    assert pick["safe"] == "H" and pick["risk"] == "A" and pick["tide"] == "D"


def test_leaderboard_shows_all_six_when_no_bets(tmp_path):
    # Before any match day there are no bets, but all six players still appear at zero.
    init_db(tmp_path / "e.db")
    with connect(tmp_path / "e.db") as conn:
        board = leaderboard(conn)
    assert {r["player"] for r in board} == {
        "model_full", "model_fundamental", "safe", "tide", "risk", "monkey"}
    for r in board:
        assert r["staked"] == 0.0 and r["n_bets"] == 0 and r["roi"] is None


def test_settle_pays_winners_only(tmp_path):
    db = _seed(tmp_path / "b.db")
    with connect(db) as conn:
        place_bets(conn, as_of=MATCH_DAY)
        conn.execute("UPDATE matches SET home_goals=2, away_goals=0, status='final' "
                     "WHERE match_id=100")            # Mexico win -> outcome H
        assert settle_bets(conn) == 6
        assert settle_bets(conn) == 0
        rows = conn.execute("SELECT pick, odd, payout FROM bets").fetchall()
    for r in rows:
        assert r["payout"] == (STAKE_NIS * r["odd"] if r["pick"] == "H" else 0.0)


def test_invested_counts_before_results(tmp_path):
    # A placed-but-unsettled bet is already "paid": it shows in staked with 0 back.
    db = _seed(tmp_path / "b.db")
    with connect(db) as conn:
        place_bets(conn, as_of=MATCH_DAY)
        board = {r["player"]: r for r in leaderboard(conn)}
    safe = board["safe"]
    assert safe["staked"] == 10.0 and safe["returned"] == 0.0
    assert safe["profit"] == -10.0 and safe["roi"] == -1.0   # paid, nothing back yet
    assert safe["n_settled"] == 0


def test_leaderboard_revenue_over_invested(tmp_path):
    db = _seed(tmp_path / "b.db")
    with connect(db) as conn:
        place_bets(conn, as_of=MATCH_DAY)
        conn.execute("UPDATE matches SET home_goals=2, away_goals=0, status='final' "
                     "WHERE match_id=100")
        settle_bets(conn)
        by = {r["player"]: r for r in leaderboard(conn)}
    # safe bet H @1.40, home win -> invested 10, returned 14, revenue +4, roi 40%.
    assert by["safe"]["staked"] == 10.0 and by["safe"]["returned"] == 14.0
    assert by["safe"]["profit"] == 4.0 and by["safe"]["roi"] == 0.4
    assert by["safe"]["wins"] == 1 and by["safe"]["hit_rate"] == 1.0
    assert by["safe"]["best_bet"]["pick"] == "Mexico"
    # tide bet D and lost -> invested 10, returned 0, revenue -10, roi -100%.
    assert by["tide"]["profit"] == -10.0 and by["tide"]["roi"] == -1.0
    assert by["tide"]["wins"] == 0 and by["tide"]["best_bet"] is None
