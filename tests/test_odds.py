"""Odds loaders: Winner-strict eval price vs sharp 'true market' reference."""
import sqlite3

from mondial.eval.odds import load_eval_odds, load_match_odds


def _conn(rows):
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE odds_snapshots (match_id INT, bookmaker TEXT, ts TEXT, "
              "odd_home REAL, odd_draw REAL, odd_away REAL)")
    c.executemany("INSERT INTO odds_snapshots VALUES (?,?,?,?,?,?)", rows)
    return c


def test_load_eval_odds_picks_latest_winner_row():
    c = _conn([
        (1, "pinnacle", "2026-06-10T10:00", 1.50, 4.00, 6.00),
        (1, "winner", "2026-06-10T09:00", 1.60, 3.80, 5.50),
        (1, "winner", "2026-06-11T08:00", 1.55, 3.90, 5.80),  # newest -> chosen
    ])
    res = load_eval_odds(c, 1)
    assert res is not None
    odds, book = res
    assert book == "winner"
    assert (odds.odd_home, odds.odd_draw, odds.odd_away) == (1.55, 3.90, 5.80)


def test_load_eval_odds_is_strict_no_fallback():
    # A sharp book exists but NO winner row -> strict loader returns None (the
    # benchmark price must be the benchmark's; never settle at a different book).
    c = _conn([(1, "pinnacle", "t", 1.50, 4.00, 6.00)])
    assert load_eval_odds(c, 1) is None


def test_load_match_odds_prefers_sharp_book():
    c = _conn([
        (1, "williamhill", "t", 1.40, 4.20, 7.00),
        (1, "pinnacle", "t", 1.50, 4.00, 6.00),
    ])
    res = load_match_odds(c, 1)
    assert res is not None and res[1] == "pinnacle"
