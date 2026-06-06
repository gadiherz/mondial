"""Web export: bankroll (cumulative-profit) time-series for the revenue chart."""
from mondial.data.db import connect, init_db
from mondial.pipelines.export_web import _bankroll


def test_bankroll_empty_when_nothing_settled(tmp_path):
    init_db(tmp_path / "e.db")
    with connect(tmp_path / "e.db") as c:
        bk = _bankroll(c)
    assert bk["n"] == 0 and bk["series"] == {}


def test_bankroll_is_cumulative_in_time_order(tmp_path):
    db = tmp_path / "b.db"
    init_db(db)
    with connect(db) as c:
        c.execute("INSERT INTO teams(team_id,name,confederation) VALUES (1,'A','X'),(2,'B','X')")
        c.execute("INSERT INTO matches(match_id,date,home_id,away_id,tournament,"
                  "home_goals,away_goals,status) VALUES "
                  "(10,'2026-06-11',1,2,'WC',2,0,'final'),"
                  "(11,'2026-06-12',1,2,'WC',0,1,'final')")
        # 'safe' backs Home both times @1.50: match10 wins (+5), match11 loses (−10).
        c.execute("INSERT INTO bets(player_name,match_id,pick,stake,odd,payout,settled) "
                  "VALUES ('safe',10,'H',10,1.5,15,1),('safe',11,'H',10,1.5,0,1)")
        bk = _bankroll(c)
    assert bk["n"] == 2
    assert bk["labels"] == ["2026-06-11", "2026-06-12"]
    assert bk["series"]["safe"] == [5.0, -5.0]      # +5, then +5−10
    assert bk["series"]["monkey"] == [0.0, 0.0]      # placed no bets -> flat line
