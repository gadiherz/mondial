"""Live six-player betting simulation -> the `bets` table (the leaderboard's data).

Distinct from eval/backtest.py (which scores historical tournaments in-memory):
this PLACES real virtual bets for upcoming WC-2026 matches at the Winner line and
SETTLES them once results are final, persisting every bet so the dashboard can show
running bankroll / ROI per player. See ARCHITECTURE.md §7.

Flow (the two routines call these):
  place_bets   (Routine A, pre-kickoff): for each scheduled match that has BOTH a
               model prediction and Winner odds, each of the six players stakes a
               flat 10 NIS on its pick at the Winner price. Idempotent -- one bet
               per (player, match), locked at first placement (INSERT OR IGNORE on
               the unique index), so re-runs never duplicate or re-price.
  settle_bets  (Routine B, post-match): for every still-open bet whose match is now
               final, compute the payout (stake*odd if the pick hit, else 0).

Picks come from eval/simulator.pick_per_player: model_full / model_fundamental
value-pick vs the Winner line (identical in V1 -- no market feature is wired); safe
= lowest Winner odd; tide = draw; risk = highest Winner odd; monkey = uniform random
seeded PER MATCH (Random(match_id)) so it is reproducible across incremental runs.
The eval book is Winner throughout (eval/odds.load_eval_odds), the benchmark the
players bet and settle against (ARCHITECTURE D2).
"""
from __future__ import annotations

import logging
import random
import sqlite3

from mondial.config import MIN_PROB_EDGE
from mondial.eval.odds import load_eval_odds
from mondial.eval.simulator import (
    ModelProbs,
    STAKE_NIS,
    actual_outcome,
    pick_per_player,
    settle,
)

log = logging.getLogger("betting")

# Display order for the leaderboard.
PLAYERS = ("model_full", "model_fundamental", "safe", "tide", "risk", "monkey")


def place_bets(conn: sqlite3.Connection) -> int:
    """Place the six players' bets on every priced, predicted, scheduled match.

    A match is bet only when it has BOTH a `predictions` row and Winner odds. Each
    bet is locked at its Winner price at first placement; re-runs are no-ops for
    already-placed (player, match) pairs. Returns the number of NEW bets written.
    """
    rows = conn.execute(
        """SELECT pr.match_id, pr.p_home, pr.p_draw, pr.p_away
           FROM predictions pr
           JOIN matches m ON m.match_id = pr.match_id
           WHERE m.status = 'scheduled'
           ORDER BY m.date, pr.match_id"""
    ).fetchall()

    placed = priced = 0
    for r in rows:
        res = load_eval_odds(conn, r["match_id"])   # Winner odds, strict
        if res is None:
            continue
        wodds, _ = res
        priced += 1
        mp = ModelProbs(r["p_home"], r["p_draw"], r["p_away"])
        # Per-match seed -> monkey is deterministic regardless of when it's placed.
        rng = random.Random(r["match_id"])
        picks = pick_per_player(wodds, mp, mp, rng, MIN_PROB_EDGE)
        for player, pick in picks.items():
            cur = conn.execute(
                """INSERT OR IGNORE INTO bets
                   (player_name, match_id, pick, stake, odd, settled)
                   VALUES (?, ?, ?, ?, ?, 0)""",
                (player, r["match_id"], pick, STAKE_NIS, wodds.by_outcome(pick)),
            )
            placed += cur.rowcount
    log.info("place_bets: %d new bets across %d priced matches (6 players each)",
             placed, priced)
    return placed


def settle_bets(conn: sqlite3.Connection) -> int:
    """Settle every open bet whose match is now final. Returns bets settled."""
    rows = conn.execute(
        """SELECT b.bet_id, b.pick, b.stake, b.odd, m.home_goals, m.away_goals
           FROM bets b
           JOIN matches m ON m.match_id = b.match_id
           WHERE b.settled = 0 AND m.status = 'final'
             AND m.home_goals IS NOT NULL AND m.away_goals IS NOT NULL"""
    ).fetchall()

    settled = 0
    for r in rows:
        actual = actual_outcome(int(r["home_goals"]), int(r["away_goals"]))
        payout = settle(r["pick"], actual, r["odd"], r["stake"])
        conn.execute("UPDATE bets SET payout = ?, settled = 1 WHERE bet_id = ?",
                     (payout, r["bet_id"]))
        settled += 1
    log.info("settle_bets: %d bets settled", settled)
    return settled


def _pick_label(home: str, away: str, pick: str) -> str:
    return home if pick == "H" else "Draw" if pick == "D" else away


def _best_bets(conn: sqlite3.Connection) -> dict[str, dict]:
    """Each player's single most profitable settled winning bet ('best guess')."""
    best: dict[str, dict] = {}
    for r in conn.execute(
        """SELECT b.player_name, b.pick, b.odd, b.stake, b.payout,
                  th.name AS home, ta.name AS away
           FROM bets b
           JOIN matches m ON m.match_id = b.match_id
           JOIN teams th ON th.team_id = m.home_id
           JOIN teams ta ON ta.team_id = m.away_id
           WHERE b.settled = 1 AND b.payout > 0
           ORDER BY b.payout DESC"""
    ):
        if r["player_name"] not in best:        # first = highest payout for that player
            best[r["player_name"]] = {
                "match": f'{r["home"]} v {r["away"]}',
                "pick": _pick_label(r["home"], r["away"], r["pick"]),
                "odd": r["odd"],
                "profit": round(r["payout"] - r["stake"], 2),
            }
    return best


def leaderboard(conn: sqlite3.Connection) -> list[dict]:
    """Per-player standings. ROI / hit-rate are on SETTLED bets only (open bets have
    no payout yet); `staked` is total exposure. `best_bet` = most profitable win."""
    rows = conn.execute(
        """SELECT player_name,
                  COUNT(*)                                        AS n_bets,
                  SUM(settled)                                    AS n_settled,
                  SUM(CASE WHEN settled=1 AND payout>0 THEN 1 ELSE 0 END) AS wins,
                  SUM(stake)                                      AS staked,
                  SUM(CASE WHEN settled = 1 THEN stake ELSE 0 END) AS settled_stake,
                  SUM(COALESCE(payout, 0.0))                      AS returned
           FROM bets GROUP BY player_name"""
    ).fetchall()
    by_player = {r["player_name"]: r for r in rows}
    best = _best_bets(conn)

    board = []
    for name in (*PLAYERS, *(p for p in by_player if p not in PLAYERS)):
        r = by_player.get(name)
        if r is None:
            continue
        settled_stake = float(r["settled_stake"] or 0.0)
        returned = float(r["returned"] or 0.0)
        profit = returned - settled_stake
        n_settled = int(r["n_settled"] or 0)
        wins = int(r["wins"] or 0)
        board.append({
            "player": name,
            "n_bets": int(r["n_bets"]),
            "n_settled": n_settled,
            "wins": wins,
            "hit_rate": round(wins / n_settled, 4) if n_settled else None,
            "staked": float(r["staked"] or 0.0),
            "returned": round(returned, 2),
            "profit": round(profit, 2),
            "roi": round(profit / settled_stake, 4) if settled_stake else None,
            "best_bet": best.get(name),
        })
    return board
