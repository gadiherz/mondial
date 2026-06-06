"""Quick inspection of bootstrap output. Not part of the package."""
from mondial.data.db import connect

with connect() as conn:
    print("Top 12 Elo:")
    for r in conn.execute(
        """SELECT t.name, r.elo FROM ratings r
           JOIN teams t ON r.team_id=t.team_id
           WHERE r.elo IS NOT NULL
           ORDER BY r.elo DESC LIMIT 12"""
    ):
        print(f"  {r['name']:<28} {r['elo']:7.1f}")

    print("\nScheduled WC 2026 fixtures (first 8):")
    for r in conn.execute(
        """SELECT m.date, h.name AS home, a.name AS away, m.tournament
           FROM matches m
           JOIN teams h ON m.home_id=h.team_id
           JOIN teams a ON m.away_id=a.team_id
           WHERE m.status='scheduled' AND m.date >= '2026-06-11'
           ORDER BY m.date LIMIT 8"""
    ):
        print(f"  {r['date']}  {r['home']:<22} vs {r['away']:<22}  ({r['tournament']})")

    n_sched_wc = conn.execute(
        "SELECT COUNT(*) AS n FROM matches WHERE status='scheduled' AND date >= '2026-06-11'"
    ).fetchone()["n"]
    print(f"\nTotal scheduled fixtures >= 2026-06-11: {n_sched_wc}")
