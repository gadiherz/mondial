"""Export the static-site data feed -> web/data/dashboard.json.

The static frontend reads ONLY this file (via fetch); it never touches the DB,
secrets, or a server. The file holds PUBLIC match data only -- predictions, odds,
results, the six-player leaderboard, and the countdown target times -- so it is
safe to publish. Written at the end of the routines (after predict / results) so
the deployed site refreshes after every match.

Run: PYTHONPATH=src python -m mondial.pipelines.export_web
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta

from mondial.config import MIN_PROB_EDGE, ROOT
from mondial.data.countries import code_for
from mondial.data.db import connect
from mondial.eval.betting import PLAYERS, leaderboard
from mondial.eval.odds import devig, load_eval_odds
from mondial.eval.simulator import ModelProbs, value_pick

log = logging.getLogger("export_web")

WEB_DATA = ROOT / "web" / "data" / "dashboard.json"

# Countdown targets. Routine A (markets-intel) cron is 14:00 UTC daily -- the
# "daily scrape + value calc" the UI counts down to. KICKOFF is the opener; the
# exact minute is worth confirming (the DB stores match dates without a time).
SCRAPE_CRON_UTC_HOUR = 14
KICKOFF_UTC = "2026-06-11T19:00:00Z"

# Matches to surface: the FULL results history (every finished WC match) plus the
# next UPCOMING_LIMIT scheduled fixtures. Finished matches are kept indefinitely so
# the Results tab is a complete archive, not a rolling window.
# CRUCIAL: `tournament='WC'` also tags the historical World Cups used for training
# (2014/2018/2022), so finals are scoped to this edition by date (>= WC_SEASON_START)
# to keep decades of old results out of the feed.
UPCOMING_LIMIT = 30
WC_SEASON_START = KICKOFF_UTC[:4] + "-01-01"   # e.g. "2026-01-01"
# The per-match "why this prediction" breakdown is the bulk of the feed (~3 KB
# each). Keep it only for upcoming matches and recently-finished ones, so the
# growing results archive doesn't bloat the feed; older finals still show their
# result + hit/miss, just without the explain panel.
BREAKDOWN_DAYS = 7

OUTCOMES = ("H", "D", "A")


def _now() -> datetime:
    return datetime.now(UTC)


def _next_scrape(now: datetime) -> str:
    target = now.replace(hour=SCRAPE_CRON_UTC_HOUR, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target.isoformat().replace("+00:00", "Z")


def _result(hg, ag) -> str | None:
    if hg is None or ag is None:
        return None
    return "H" if hg > ag else "A" if hg < ag else "D"


def _matches(conn) -> list[dict]:
    # Every finished WC match (full archive) + the next UPCOMING_LIMIT scheduled.
    recent_cutoff = (_now().date() - timedelta(days=BREAKDOWN_DAYS)).isoformat()
    rows = conn.execute(
        """SELECT m.match_id, m.date, m.stage, m.neutral, m.status,
                  m.home_goals, m.away_goals, m.kickoff_utc,
                  h.name AS home, a.name AS away
           FROM matches m
           JOIN teams h ON h.team_id = m.home_id
           JOIN teams a ON a.team_id = m.away_id
           WHERE m.tournament='WC' AND (
                 (m.status='final' AND m.date >= ?)
                 OR m.match_id IN (
                     SELECT match_id FROM matches
                     WHERE tournament='WC' AND status != 'final'
                     ORDER BY date, match_id LIMIT ?))
           ORDER BY m.date, m.match_id""",
        (WC_SEASON_START, UPCOMING_LIMIT),
    ).fetchall()

    out = []
    for r in rows:
        pred = conn.execute(
            "SELECT p_home, p_draw, p_away FROM predictions WHERE match_id=?",
            (r["match_id"],)).fetchone()
        item = {
            "date": r["date"], "stage": r["stage"] or "group",
            "home": r["home"], "away": r["away"],
            "home_code": code_for(r["home"]), "away_code": code_for(r["away"]),
            "venue": None,
            "status": r["status"],
            "kickoff_utc": r["kickoff_utc"],   # drives the Cloudflare results-trigger worker
            "result": _result(r["home_goals"], r["away_goals"]),
            "score": ([r["home_goals"], r["away_goals"]]
                      if r["home_goals"] is not None else None),
            "model": None, "winner_odds": None, "model_pick": None,
        }
        if pred is not None:
            p = (pred["p_home"], pred["p_draw"], pred["p_away"])
            item["model"] = {"home": round(p[0], 3), "draw": round(p[1], 3),
                             "away": round(p[2], 3)}
            odds_res = load_eval_odds(conn, r["match_id"])
            if odds_res is not None:
                o, _ = odds_res
                tp = devig(o)
                item["winner_odds"] = {"home": o.odd_home, "draw": o.odd_draw,
                                       "away": o.odd_away,
                                       "true": [round(x, 3) for x in tp]}
                item["model_pick"] = value_pick(ModelProbs(*p), o, MIN_PROB_EDGE)
            else:
                item["model_pick"] = OUTCOMES[max(range(3), key=lambda i: p[i])]
            # Attach the per-match "why this prediction" breakdown for upcoming and
            # recently-finished matches only (it's the bulk of the feed; older finals
            # keep their result but drop the explain panel -- the frontend omits it
            # gracefully). Absent too for matches predicted before the feature existed.
            if r["status"] != "final" or r["date"] >= recent_cutoff:
                bd = conn.execute(
                    "SELECT json FROM match_breakdown WHERE match_id=?",
                    (r["match_id"],)).fetchone()
            else:
                bd = None
            if bd is not None:
                try:
                    item["breakdown"] = json.loads(bd["json"])
                except (ValueError, TypeError):
                    log.warning("export: bad breakdown JSON for match_id=%s; omitting",
                                r["match_id"])
        out.append(item)
    return out


def _winner_health(conn, matches: list[dict]) -> dict:
    """Visibility canary for the auto-scraped Winner line.

    A silent scrape failure (0 cards swallowed by @step, or the bankerim line
    posted after the daily run) used to surface only as `winner_odds: null` on
    individual matches. This summarises it so the frontend can show a banner --
    per the project convention that a degraded/stale automated feed must announce
    itself, not fail quietly.
    """
    row = conn.execute(
        "SELECT MAX(ts) AS ts FROM odds_snapshots WHERE bookmaker='winner'"
    ).fetchone()
    last_ts = row["ts"] if row else None
    stale_hours = None
    if last_ts:
        try:
            dt = datetime.fromisoformat(last_ts)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            stale_hours = round((_now() - dt).total_seconds() / 3600, 1)
        except ValueError:
            pass
    missing = sum(1 for m in matches
                  if m["status"] != "final" and m["winner_odds"] is None)
    return {"last_winner_ts": last_ts, "stale_hours": stale_hours,
            "upcoming_missing_count": missing}


def _ratings_through(conn) -> str | None:
    """Date of the latest FINAL match folded into the Glicko/momentum ratings.

    Lets the UI show how current the model inputs are. Glicko-2 + momentum update
    from every settled match (Routine B); the Dixon-Coles coefficients and the
    calibration temperature are frozen at tournament start by design (the WC sample
    is too small to re-fit against the 10k-match prior), so daily prediction
    movement comes from these rating/momentum inputs, not from re-fitting."""
    row = conn.execute(
        "SELECT MAX(date) AS d FROM matches WHERE status='final'").fetchone()
    return row["d"] if row else None


def _bankroll(conn) -> dict:
    """Cumulative profit per player across settled matches, in time order.

    Feeds the revenue-vs-match-count chart: x = settled-match index, y = running
    profit. Empty until matches are settled. All six players bet the same matches,
    so they share one x-axis; a player with no bet on a match carries its line flat.
    """
    matches = conn.execute(
        """SELECT DISTINCT b.match_id, m.date FROM bets b
           JOIN matches m ON m.match_id = b.match_id
           WHERE b.settled = 1 ORDER BY m.date, b.match_id"""
    ).fetchall()
    if not matches:
        return {"n": 0, "labels": [], "series": {}}

    profit: dict[tuple, float] = {}
    for r in conn.execute(
        "SELECT player_name, match_id, payout, stake FROM bets WHERE settled = 1"
    ):
        profit[(r["player_name"], r["match_id"])] = (r["payout"] or 0.0) - r["stake"]

    cum = {p: 0.0 for p in PLAYERS}
    series: dict[str, list] = {p: [] for p in PLAYERS}
    labels = []
    for m in matches:
        labels.append(m["date"])
        for p in PLAYERS:
            cum[p] += profit.get((p, m["match_id"]), 0.0)
            series[p].append(round(cum[p], 2))
    return {"n": len(matches), "labels": labels, "series": series}


def _player_bets(conn) -> dict[str, list]:
    """Per-player bet history for the players-page drill-down: every bet, its pick,
    whether it won/lost (or is still pending), and the money made on it.

    Keyed by player_name, newest match first. profit = payout - stake (settled
    bets only); pending bets carry profit=None and won=None.
    """
    rows = conn.execute(
        """SELECT b.player_name, b.pick, b.stake, b.odd, b.payout, b.settled,
                  m.date, m.home_goals, m.away_goals,
                  h.name AS home, a.name AS away
           FROM bets b
           JOIN matches m ON m.match_id = b.match_id
           JOIN teams h ON h.team_id = m.home_id
           JOIN teams a ON a.team_id = m.away_id
           ORDER BY m.date DESC, b.bet_id DESC"""
    ).fetchall()
    out: dict[str, list] = {}
    for r in rows:
        settled = bool(r["settled"])
        won = bool((r["payout"] or 0.0) > 0) if settled else None
        profit = round((r["payout"] or 0.0) - r["stake"], 2) if settled else None
        out.setdefault(r["player_name"], []).append({
            "date": r["date"], "home": r["home"], "away": r["away"],
            "pick": r["pick"],                                   # 'H' | 'D' | 'A'
            "result": _result(r["home_goals"], r["away_goals"]),  # match outcome or None
            "score": ([r["home_goals"], r["away_goals"]]
                      if r["home_goals"] is not None else None),
            "stake": r["stake"], "odd": r["odd"],
            "payout": r["payout"], "profit": profit,
            "settled": settled, "won": won,
        })
    return out


def export() -> dict:
    """Build the dashboard payload and write it to web/data/dashboard.json."""
    now = _now()
    with connect() as conn:
        board = leaderboard(conn)
        history = _player_bets(conn)
        for row in board:                       # drill-down bet list per player card
            row["bets"] = history.get(row["player"], [])
        matches = _matches(conn)
        payload = {
            "meta": {
                "generated_at": now.isoformat().replace("+00:00", "Z"),
                "kickoff_utc": KICKOFF_UTC,
                "next_scrape_utc": _next_scrape(now),
                "ratings_through": _ratings_through(conn),
            },
            "matches": matches,
            "winner_odds_health": _winner_health(conn, matches),
            "leaderboard": board,
            "bankroll": _bankroll(conn),
        }
    WEB_DATA.parent.mkdir(parents=True, exist_ok=True)
    WEB_DATA.write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                        encoding="utf-8")
    log.info("export_web: wrote %s (%d matches, %d players)",
             WEB_DATA, len(payload["matches"]), len(payload["leaderboard"]))
    return payload


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    export()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
