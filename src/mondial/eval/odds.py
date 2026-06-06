"""Read stored HDA odds for a match and convert to a value-pick price.

`load_match_odds` returns the latest snapshot from the sharpest available
bookmaker (Pinnacle first -- lowest margin, closest to "true" odds). The value
picker (eval/simulator.value_pick) consumes the RAW decimal odds; `devig` gives
the margin-free implied probabilities for display ("the market's true HDA").
"""
from __future__ import annotations

import sqlite3

from mondial.eval.simulator import MatchOdds

# Preference order: sharpest / lowest-margin books first.
SHARP_PREFERENCE: tuple[str, ...] = (
    "pinnacle", "betfair_ex_eu", "betfair", "marathonbet", "matchbook",
)

# The eval benchmark book. WINNER is the only legal Israeli sportsbook, so its
# fixed 1X2 line is the WINNER-16 reference the six-player sim picks and settles
# against (ARCHITECTURE D2). Distinct from SHARP_PREFERENCE, which stays the sharp
# "true market" reference used to measure the model's edge.
EVAL_BOOKMAKER = "winner"


def load_match_odds(
    conn: sqlite3.Connection,
    match_id: int,
    prefer: tuple[str, ...] = SHARP_PREFERENCE,
) -> tuple[MatchOdds, str] | None:
    """Latest complete HDA snapshot for `match_id`, preferring sharp books.

    Returns (MatchOdds, bookmaker_key) or None if no usable odds are stored.
    """
    rows = conn.execute(
        """SELECT bookmaker, ts, odd_home, odd_draw, odd_away
           FROM odds_snapshots
           WHERE match_id=? AND odd_home IS NOT NULL
             AND odd_draw IS NOT NULL AND odd_away IS NOT NULL""",
        (match_id,),
    ).fetchall()
    if not rows:
        return None

    latest: dict[str, sqlite3.Row] = {}
    for r in rows:  # keep the newest snapshot per bookmaker
        b = r["bookmaker"]
        if b not in latest or r["ts"] > latest[b]["ts"]:
            latest[b] = r

    chosen = next((latest[b] for b in prefer if b in latest), None)
    if chosen is None:  # no preferred book -> most recent of whatever we have
        chosen = max(latest.values(), key=lambda r: r["ts"])

    return (
        MatchOdds(chosen["odd_home"], chosen["odd_draw"], chosen["odd_away"]),
        chosen["bookmaker"],
    )


def load_eval_odds(
    conn: sqlite3.Connection,
    match_id: int,
    bookmaker: str = EVAL_BOOKMAKER,
) -> tuple[MatchOdds, str] | None:
    """Latest complete 1X2 odds from the EVAL benchmark book (Winner by default).

    STRICT: returns None if that specific book has no complete row for the match.
    Unlike `load_match_odds`, it never falls back to a different bookmaker -- the
    benchmark price must come from the benchmark itself, or the match is simply not
    eval-priced yet (the caller skips it rather than settling at the wrong book).
    """
    row = conn.execute(
        """SELECT odd_home, odd_draw, odd_away FROM odds_snapshots
           WHERE match_id=? AND bookmaker=? AND odd_home IS NOT NULL
             AND odd_draw IS NOT NULL AND odd_away IS NOT NULL
           ORDER BY ts DESC LIMIT 1""",
        (match_id, bookmaker),
    ).fetchone()
    if row is None:
        return None
    return MatchOdds(row["odd_home"], row["odd_draw"], row["odd_away"]), bookmaker


def devig(odds: MatchOdds) -> tuple[float, float, float]:
    """Margin-free implied (P_home, P_draw, P_away) from decimal HDA odds.

    Raw implied probs are 1/odd; their sum exceeds 1 by the bookmaker's
    overround, so we normalise to sum to 1.
    """
    raw = [1.0 / odds.odd_home, 1.0 / odds.odd_draw, 1.0 / odds.odd_away]
    s = sum(raw)
    return raw[0] / s, raw[1] / s, raw[2] / s
