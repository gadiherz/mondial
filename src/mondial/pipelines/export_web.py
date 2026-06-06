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

# Matches to surface: recent results (last PAST_DAYS) + the next UPCOMING fixtures.
# Always populated -- before kickoff it shows the opening slate; during the
# tournament, recent + upcoming.
PAST_DAYS = 2
UPCOMING_LIMIT = 30

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
    today = _now().date()
    lo = (today - timedelta(days=PAST_DAYS)).isoformat()
    rows = conn.execute(
        """SELECT m.match_id, m.date, m.stage, m.neutral, m.status,
                  m.home_goals, m.away_goals,
                  h.name AS home, a.name AS away
           FROM matches m
           JOIN teams h ON h.team_id = m.home_id
           JOIN teams a ON a.team_id = m.away_id
           WHERE m.tournament='WC' AND m.date >= ?
           ORDER BY m.date, m.match_id
           LIMIT ?""",
        (lo, UPCOMING_LIMIT),
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
        out.append(item)
    return out


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


def export() -> dict:
    """Build the dashboard payload and write it to web/data/dashboard.json."""
    now = _now()
    with connect() as conn:
        payload = {
            "meta": {
                "generated_at": now.isoformat().replace("+00:00", "Z"),
                "kickoff_utc": KICKOFF_UTC,
                "next_scrape_utc": _next_scrape(now),
            },
            "matches": _matches(conn),
            "leaderboard": leaderboard(conn),
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
