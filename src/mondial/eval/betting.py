"""Live six-player betting simulation -> the `bets` table (the leaderboard's data).

Distinct from eval/backtest.py (which scores historical tournaments in-memory):
this PLACES real virtual bets for upcoming WC-2026 matches at the Winner line and
SETTLES them once results are final, persisting every bet so the dashboard can show
running bankroll / ROI per player. See ARCHITECTURE.md §7.

Flow (the two routines call these):
  place_bets   (Routine A, each match day): for each scheduled, priced match whose
               MATCH DAY has arrived (date <= today), the six players each stake a
               flat 10 NIS on its pick at the Winner price. The match-day guard
               means "invested" tracks match-days, never how often the routine runs
               (no pre-betting future matches). Idempotent -- one bet per (player,
               match), locked at first placement (INSERT OR IGNORE).
  settle_bets  (Routine B, post-match): for every still-open bet whose match is now
               final, compute the payout (stake*odd if the pick hit, else 0).

Picks come from eval/simulator.pick_per_player: model_full value-picks vs the
Winner line (with a relaxed edge floor for draws); model_fundamental backs the
model's most-likely outcome but DRAW-AWARE (argmax_draw_aware: picks the draw when
its probability clears DRAW_THRESHOLD, so it doesn't cede every X), ignoring the
market; safe = lowest Winner odd; tide = draw; risk = highest Winner
odd; monkey = uniform random over H/D/A, seeded PER MATCH with a STRING (so
consecutive match_ids decorrelate -- a raw int seed left the Mersenne Twister's
first draw correlated and the monkey almost never picked the draw). Reproducible
across incremental runs.
The eval book is Winner throughout (eval/odds.load_eval_odds), the benchmark the
players bet and settle against (ARCHITECTURE D2).
"""
from __future__ import annotations

import logging
import random
import sqlite3
from datetime import UTC, datetime

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


def place_bets(conn: sqlite3.Connection, *, as_of: str | None = None) -> int:
    """Place the six players' bets for matches whose MATCH DAY has arrived.

    The guard (this is the key rule): a match is bet only when it is priced (has
    Winner odds) AND its date is on/before `as_of` (default = today, UTC). This ties
    each player's invested total to MATCH-DAYS, not to how often the predict/intel
    routine runs: future matches are never pre-bet (the date guard), so "risked"
    only grows as match-days arrive (each day a player effectively pays 10 NIS x
    that day's matches). Each bet is locked at its Winner price at first placement;
    idempotent per (player, match). Returns the number of NEW bets written.

    Both `scheduled` and `final` matches are eligible: a match-day that arrived is a
    bet the players should have made, even if the match has since finished. This
    backfills any match that was priced late -- e.g. its Winner line landed only
    after kickoff -- so a scrape gap can't silently drop it from the standings.
    `INSERT OR IGNORE` keeps already-bet matches untouched, and the strict odds
    check below still excludes any final that was never priced.
    """
    today = as_of or datetime.now(UTC).date().isoformat()
    rows = conn.execute(
        """SELECT pr.match_id, pr.p_home, pr.p_draw, pr.p_away
           FROM predictions pr
           JOIN matches m ON m.match_id = pr.match_id
           WHERE m.status IN ('scheduled', 'final') AND m.date <= ?
           ORDER BY m.date, pr.match_id""",
        (today,),
    ).fetchall()

    placed = priced = 0
    for r in rows:
        res = load_eval_odds(conn, r["match_id"])   # Winner odds, strict
        if res is None:
            continue
        wodds, _ = res
        priced += 1
        mp = ModelProbs(r["p_home"], r["p_draw"], r["p_away"])
        # Per-match seed (a STRING, so Python mixes it through SHA-512 and
        # consecutive match_ids don't correlate) -> the monkey is deterministic
        # regardless of when it's placed, AND uniform over H/D/A. A raw int seed
        # left the first draw biased (Away-heavy, almost never the draw).
        rng = random.Random(f"monkey-{r['match_id']}")
        picks = pick_per_player(wodds, mp, mp, rng, MIN_PROB_EDGE)
        for player, pick in picks.items():
            cur = conn.execute(
                """INSERT OR IGNORE INTO bets
                   (player_name, match_id, pick, stake, odd, settled)
                   VALUES (?, ?, ?, ?, ?, 0)""",
                (player, r["match_id"], pick, STAKE_NIS, wodds.by_outcome(pick)),
            )
            placed += cur.rowcount
    log.info("place_bets: %d new bets across %d priced match-day matches (<= %s)",
             placed, priced, today)
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
    """Per-player standings using the match-day investment model.

    `staked` (invested) = 10 NIS x every bet placed -- and since placement is
    match-day-guarded (see place_bets), that is exactly "10 x matches whose day has
    arrived". `returned` = total payout from settled bets. **revenue/profit =
    returned - staked** (money out vs money back, accruing per match), and
    **roi = profit / staked**. So a bet placed today but not yet settled counts as
    paid (in `staked`) with 0 returned until its result lands. hit-rate is over
    settled bets; `best_bet` = most profitable win.
    """
    rows = conn.execute(
        """SELECT player_name,
                  COUNT(*)                                        AS n_bets,
                  SUM(settled)                                    AS n_settled,
                  SUM(CASE WHEN settled=1 AND payout>0 THEN 1 ELSE 0 END) AS wins,
                  SUM(stake)                                      AS staked,
                  SUM(COALESCE(payout, 0.0))                      AS returned
           FROM bets GROUP BY player_name"""
    ).fetchall()
    by_player = {r["player_name"]: r for r in rows}
    best = _best_bets(conn)

    board = []
    for name in (*PLAYERS, *(p for p in by_player if p not in PLAYERS)):
        r = by_player.get(name)
        if r is None:
            # No bets yet (e.g. before any match day) -> show the player at zero,
            # so the six characters always appear on the dashboard.
            board.append({"player": name, "n_bets": 0, "n_settled": 0, "wins": 0,
                          "hit_rate": None, "staked": 0.0, "returned": 0.0,
                          "profit": 0.0, "roi": None, "best_bet": None})
            continue
        staked = float(r["staked"] or 0.0)            # invested = paid by match-day
        returned = float(r["returned"] or 0.0)
        profit = returned - staked                     # revenue
        n_settled = int(r["n_settled"] or 0)
        wins = int(r["wins"] or 0)
        board.append({
            "player": name,
            "n_bets": int(r["n_bets"]),
            "n_settled": n_settled,
            "wins": wins,
            "hit_rate": round(wins / n_settled, 4) if n_settled else None,
            "staked": staked,
            "returned": round(returned, 2),
            "profit": round(profit, 2),
            "roi": round(profit / staked, 4) if staked else None,
            "best_bet": best.get(name),
        })
    return board
