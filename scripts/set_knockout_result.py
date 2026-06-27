"""Manually correct a knockout match's 90' Winner result and/or advancing team.

Free data sources can't always auto-split a knockout that was LEVEL at 90' but then
DECIDED IN EXTRA TIME by a goal (no penalties): martj42 results.csv carries only the
extra-time score and there is no penalty-shootout row, so the true 90' result (a Draw)
and sometimes the advancing team can't be derived automatically. This sets them by
hand. The next routine then settles bets on the corrected 90' result and advances the
bracket; bets already settled on the wrong score are re-opened here so they re-settle.

Identify the match by its bracket number OR by team names (date-agnostic, since our
knockout fixtures carry a placeholder date).

Usage:
  PYTHONPATH=src python scripts/set_knockout_result.py --bracket 89 --reg D --winner France
  PYTHONPATH=src python scripts/set_knockout_result.py --home Spain --away Italy --winner Spain
  PYTHONPATH=src python scripts/set_knockout_result.py --bracket 97 --reg H   # 90' result only
"""
from __future__ import annotations

import argparse
import logging
import sys

from mondial.data.db import connect

log = logging.getLogger("set_knockout_result")


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--bracket", type=int, help="knockout bracket_no (73..104)")
    ap.add_argument("--home", help="home team name (alternative to --bracket)")
    ap.add_argument("--away", help="away team name (with --home)")
    ap.add_argument("--reg", choices=["H", "D", "A"],
                    help="the 90-minute Winner 1X2 outcome the bet settles on")
    ap.add_argument("--winner", help="team name that ADVANCES (after ET/penalties)")
    args = ap.parse_args()
    if not (args.bracket or (args.home and args.away)):
        ap.error("identify the match with --bracket N, or --home X --away Y")
    if not (args.reg or args.winner):
        ap.error("nothing to set: pass --reg and/or --winner")

    with connect() as conn:
        if args.bracket is not None:
            m = conn.execute(
                """SELECT m.match_id, m.home_id, m.away_id, th.name hn, ta.name an
                   FROM matches m JOIN teams th ON th.team_id=m.home_id
                   JOIN teams ta ON ta.team_id=m.away_id
                   WHERE m.tournament='WC' AND m.bracket_no=?""", (args.bracket,)).fetchone()
        else:
            ids = {r["name"]: r["team_id"] for r in conn.execute(
                "SELECT team_id, name FROM teams WHERE name IN (?, ?)",
                (args.home, args.away))}
            if args.home not in ids or args.away not in ids:
                log.error("team not found: %s", [t for t in (args.home, args.away) if t not in ids])
                return 2
            m = conn.execute(
                """SELECT m.match_id, m.home_id, m.away_id, th.name hn, ta.name an
                   FROM matches m JOIN teams th ON th.team_id=m.home_id
                   JOIN teams ta ON ta.team_id=m.away_id
                   WHERE m.tournament='WC' AND m.bracket_no IS NOT NULL
                     AND ((m.home_id=? AND m.away_id=?) OR (m.home_id=? AND m.away_id=?))""",
                (ids[args.home], ids[args.away], ids[args.away], ids[args.home])).fetchone()
        if m is None:
            log.error("no knockout match found for the given identifier.")
            return 2

        sets, params = [], []
        if args.reg:
            sets.append("reg_result=?"); params.append(args.reg)
        if args.winner:
            wid = {r["name"]: r["team_id"] for r in conn.execute(
                "SELECT team_id, name FROM teams WHERE name=?", (args.winner,))}.get(args.winner)
            if wid is None:
                log.error("winner team not found: %s", args.winner)
                return 2
            if wid not in (m["home_id"], m["away_id"]):
                log.error("winner %s is not in this match (%s v %s)",
                          args.winner, m["hn"], m["an"])
                return 2
            sets.append("winner_id=?"); params.append(wid)
        params.append(m["match_id"])
        conn.execute(f"UPDATE matches SET {', '.join(sets)} WHERE match_id=?", params)

        reopened = 0
        if args.reg:
            # Re-open this match's bets so they re-settle on the corrected 90' result.
            cur = conn.execute(
                "UPDATE bets SET settled=0, payout=NULL WHERE match_id=?", (m["match_id"],))
            reopened = cur.rowcount

    log.info("set: %s v %s -> reg_result=%s winner=%s (%d bets re-opened for re-settle; "
             "run `python -m mondial.pipelines.results` to apply).",
             m["hn"], m["an"], args.reg or "(unchanged)",
             args.winner or "(unchanged)", reopened)
    return 0


if __name__ == "__main__":
    sys.exit(main())
