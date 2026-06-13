"""Web export: bankroll (cumulative-profit) time-series for the revenue chart."""
from mondial.data.db import connect, init_db
from mondial.pipelines.export_web import _bankroll, _matches, _player_bets


def test_player_bets_history(tmp_path):
    """Per-player drill-down: a won bet, a lost bet, and a pending one."""
    db = tmp_path / "h.db"
    init_db(db)
    with connect(db) as c:
        c.execute("INSERT INTO teams(team_id,name,confederation) VALUES (1,'A','X'),(2,'B','X')")
        c.execute("INSERT INTO matches(match_id,date,home_id,away_id,tournament,"
                  "home_goals,away_goals,status) VALUES "
                  "(10,'2026-06-11',1,2,'WC',2,0,'final'),"      # A wins
                  "(11,'2026-06-12',1,2,'WC',0,1,'final'),"      # A loses
                  "(12,'2026-06-13',1,2,'WC',NULL,NULL,'scheduled')")  # not played
        c.execute("INSERT INTO bets(player_name,match_id,pick,stake,odd,payout,settled) VALUES "
                  "('safe',10,'H',10,1.5,15,1),"     # won:  +5
                  "('safe',11,'H',10,1.5,0,1),"      # lost: -10
                  "('safe',12,'H',10,2.0,NULL,0)")   # pending
        hist = _player_bets(c)
    bets = hist["safe"]
    assert len(bets) == 3
    assert bets[0]["date"] == "2026-06-13"          # newest first
    assert bets[0]["settled"] is False and bets[0]["won"] is None and bets[0]["profit"] is None
    won = next(b for b in bets if b["date"] == "2026-06-11")
    assert won["won"] is True and won["profit"] == 5.0 and won["score"] == [2, 0]
    lost = next(b for b in bets if b["date"] == "2026-06-12")
    assert lost["won"] is False and lost["profit"] == -10.0


def test_matches_feed_carries_kickoff_utc(tmp_path):
    """The Cloudflare results-trigger worker reads match.kickoff_utc from the feed;
    guard the contract so it can't be dropped silently."""
    from datetime import UTC, datetime
    today = datetime.now(UTC).date().isoformat()   # keep it inside the feed's recent window
    ko = today + "T19:00:00Z"
    db = tmp_path / "k.db"
    init_db(db)
    with connect(db) as c:
        c.execute("INSERT INTO teams(team_id,name,confederation) VALUES (1,'A','X'),(2,'B','X')")
        c.execute("INSERT INTO matches(match_id,date,home_id,away_id,tournament,"
                  "status,kickoff_utc) VALUES (10,?,1,2,'WC','scheduled',?)", (today, ko))
        rows = _matches(c)
    assert len(rows) == 1
    assert rows[0]["kickoff_utc"] == ko


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
