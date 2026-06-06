"""Validate the Glicko-2 engine on the real match corpus.

Streams every final match (date order) through mondial.model.glicko with
idle-time RD inflation, then reports:
  * top-15 by rating (should look like a sane world ranking),
  * the user's named black-swans vs the usual suspects: rating, RD (uncertainty),
    and volatility (momentum) -- we WANT the black-swans to carry higher RD/vol,
  * RD distribution sanity (teams that play often should be more certain).

Not part of the package; real-data sanity check only.
"""
from __future__ import annotations

from datetime import date

from mondial.data.db import connect
from mondial.model import glicko

BLACK_SWANS = ["Iceland", "Croatia", "Senegal", "South Korea", "Uruguay", "Morocco"]
USUAL = ["Brazil", "Argentina", "Spain", "Germany", "France"]


def _days(a: str, b: str) -> float:
    return (date.fromisoformat(a) - date.fromisoformat(b)).days


def main() -> None:
    with connect() as conn:
        rows = conn.execute(
            """SELECT date, home_id, away_id, home_goals, away_goals,
                      tournament, neutral
               FROM matches
               WHERE status='final'
                 AND home_goals IS NOT NULL AND away_goals IS NOT NULL
               ORDER BY date, match_id"""
        ).fetchall()
        names = {r["team_id"]: r["name"] for r in conn.execute(
            "SELECT team_id, name FROM teams")}
        played = {r["team_id"]: 0 for r in conn.execute("SELECT team_id FROM teams")}

    ratings: dict[int, glicko.Rating] = {}
    last_date: dict[int, str] = {}
    for r in rows:
        h, a = r["home_id"], r["away_id"]
        rh = ratings.get(h, glicko.Rating())
        ra = ratings.get(a, glicko.Rating())
        h_idle = _days(r["date"], last_date[h]) if h in last_date else 0.0
        a_idle = _days(r["date"], last_date[a]) if a in last_date else 0.0
        upd = glicko.update(
            home=rh, away=ra,
            home_goals=int(r["home_goals"]), away_goals=int(r["away_goals"]),
            tournament=r["tournament"], neutral=bool(r["neutral"]),
            home_idle_days=h_idle, away_idle_days=a_idle,
        )
        ratings[h], ratings[a] = upd.new_home, upd.new_away
        last_date[h] = last_date[a] = r["date"]
        played[h] += 1
        played[a] += 1

    print(f"Streamed {len(rows)} matches through Glicko-2.\n")

    print("=== TOP 15 BY RATING ===")
    top = sorted(ratings.items(), key=lambda kv: kv[1].rating, reverse=True)[:15]
    print(f"{'Team':<22}{'Rating':>8}{'RD':>7}{'Vol':>8}{'Games':>7}")
    for tid, rt in top:
        print(f"{names[tid]:<22}{rt.rating:8.0f}{rt.rd:7.1f}{rt.vol:8.4f}{played[tid]:7d}")

    name_to_id = {v: k for k, v in names.items()}

    def show(label: str, teams: list[str]) -> None:
        print(f"\n=== {label} ===")
        print(f"{'Team':<22}{'Rating':>8}{'RD':>7}{'Vol':>8}{'Games':>7}")
        for nm in teams:
            tid = name_to_id.get(nm)
            if tid is None or tid not in ratings:
                print(f"{nm:<22}  (not found)")
                continue
            rt = ratings[tid]
            print(f"{nm:<22}{rt.rating:8.0f}{rt.rd:7.1f}{rt.vol:8.4f}{played[tid]:7d}")

    show("BLACK SWANS", BLACK_SWANS)
    show("USUAL SUSPECTS", USUAL)

    rds = [rt.rd for tid, rt in ratings.items() if played[tid] >= 20]
    vols = [rt.vol for tid, rt in ratings.items() if played[tid] >= 20]
    print(f"\nRD over teams with >=20 games: min={min(rds):.1f} "
          f"median={sorted(rds)[len(rds)//2]:.1f} max={max(rds):.1f}")
    print(f"Vol over teams with >=20 games: min={min(vols):.4f} "
          f"median={sorted(vols)[len(vols)//2]:.4f} max={max(vols):.4f}")


if __name__ == "__main__":
    main()
