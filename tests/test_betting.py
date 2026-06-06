"""Live six-player betting sim: place -> settle -> leaderboard, on a temp DB."""
from mondial.data.db import connect, init_db
from mondial.eval.betting import leaderboard, place_bets, settle_bets
from mondial.eval.simulator import STAKE_NIS


def _seed(path):
    """A minimal real DB: two teams, one scheduled+priced+predicted match."""
    init_db(path)
    with connect(path) as conn:
        conn.execute("INSERT INTO teams (team_id, name, confederation) VALUES "
                     "(1,'Mexico','CONCACAF'),(2,'South Africa','CAF')")
        conn.execute("INSERT INTO matches (match_id, date, home_id, away_id, "
                     "tournament, neutral, status) VALUES "
                     "(100,'2026-06-11',1,2,'WC',0,'scheduled')")
        conn.execute("INSERT INTO predictions (match_id, p_home, p_draw, p_away, "
                     "predicted_at) VALUES (100,0.65,0.22,0.13,'t')")
        conn.execute("INSERT INTO odds_snapshots (match_id, bookmaker, ts, "
                     "odd_home, odd_draw, odd_away) VALUES "
                     "(100,'winner','t',1.40,4.00,7.00)")
    return path


def test_place_is_six_bets_and_idempotent(tmp_path):
    db = _seed(tmp_path / "b.db")
    with connect(db) as conn:
        assert place_bets(conn) == 6                 # one per player
        assert place_bets(conn) == 0                 # re-run places nothing new
        rows = conn.execute("SELECT player_name, pick, stake, odd, settled "
                            "FROM bets").fetchall()
    assert len(rows) == 6
    players = {r["player_name"] for r in rows}
    assert players == {"model_full", "model_fundamental", "safe", "tide",
                       "risk", "monkey"}
    for r in rows:
        assert r["stake"] == STAKE_NIS and r["settled"] == 0 and r["odd"] > 0
    # Mechanical baselines at odds 1.40/4.00/7.00: safe=lowest=H, risk=highest=A, tide=D.
    pick = {r["player_name"]: r["pick"] for r in rows}
    assert pick["safe"] == "H" and pick["risk"] == "A" and pick["tide"] == "D"


def test_settle_pays_winners_only(tmp_path):
    db = _seed(tmp_path / "b.db")
    with connect(db) as conn:
        place_bets(conn)
        # Mexico win 2-0 -> actual outcome H.
        conn.execute("UPDATE matches SET home_goals=2, away_goals=0, status='final' "
                     "WHERE match_id=100")
        assert settle_bets(conn) == 6
        assert settle_bets(conn) == 0                # nothing left open
        rows = conn.execute("SELECT pick, odd, payout, settled FROM bets").fetchall()
    for r in rows:
        assert r["settled"] == 1
        expected = STAKE_NIS * r["odd"] if r["pick"] == "H" else 0.0
        assert r["payout"] == expected


def test_leaderboard_math(tmp_path):
    db = _seed(tmp_path / "b.db")
    with connect(db) as conn:
        place_bets(conn)
        conn.execute("UPDATE matches SET home_goals=2, away_goals=0, status='final' "
                     "WHERE match_id=100")
        settle_bets(conn)
        board = leaderboard(conn)
    by = {r["player"]: r for r in board}
    # safe bet H @1.40 and the match was a home win -> profit = 10*1.40 - 10 = +4.
    assert by["safe"]["profit"] == 4.0
    assert by["safe"]["roi"] == 0.4
    assert by["safe"]["n_settled"] == 1
    # safe won its only bet -> hit-rate 1.0, and that's its best guess.
    assert by["safe"]["wins"] == 1 and by["safe"]["hit_rate"] == 1.0
    assert by["safe"]["best_bet"]["pick"] == "Mexico"
    assert by["safe"]["best_bet"]["profit"] == 4.0
    # tide bet D and lost -> returned 0, profit -10, roi -1.0, no win, no best bet.
    assert by["tide"]["returned"] == 0.0 and by["tide"]["roi"] == -1.0
    assert by["tide"]["wins"] == 0 and by["tide"]["hit_rate"] == 0.0
    assert by["tide"]["best_bet"] is None
