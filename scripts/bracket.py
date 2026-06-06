"""WC-2026 knockout bracket view + fixture generation.

    PYTHONPATH=src python scripts/bracket.py             # standings + qualification
    PYTHONPATH=src python scripts/bracket.py --generate  # also write knockout fixtures

Shows each group's table (from final results so far), the eight best third-placed
teams, and -- once the group stage is complete -- the resolved Round of 32. See
model/bracket.py. A no-op for fixture generation until all 72 group games are final.
"""
import argparse
import logging

from mondial.data.db import connect, init_db
from mondial.model.bracket import (
    GROUPS,
    best_thirds,
    compute_standings,
    generate_knockout,
    resolve_r32,
    validate_groups,
)


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--generate", action="store_true",
                    help="insert knockout fixtures determined so far")
    args = ap.parse_args()

    init_db()
    with connect() as conn:
        validate_groups(conn)                      # draw/labels sanity check
        from mondial.model.bracket import WC2026_START
        n_final = conn.execute(
            "SELECT COUNT(*) n FROM matches WHERE tournament='WC' AND bracket_no IS "
            "NULL AND status='final' AND date >= ?", (WC2026_START,)).fetchone()["n"]
        print(f"group-stage results final: {n_final}/72\n")

        standings = compute_standings(conn)
        for g, recs in standings.items():
            print(f"Group {g}")
            for pos, r in enumerate(recs, 1):
                print(f"  {pos}. {r.team:<22} P{r.played} {r.w}-{r.d}-{r.l} "
                      f"GF{r.gf} GA{r.ga} GD{r.gd:+d} {r.pts}pts")
        print("\nBest 8 third-placed teams:")
        for i, (grp, r) in enumerate(best_thirds(standings), 1):
            print(f"  {i}. (Grp {grp}) {r.team:<22} {r.pts}pts GD{r.gd:+d} GF{r.gf}")

        if n_final >= 72:
            print("\nRound of 32:")
            for mno, (home, away) in sorted(resolve_r32(conn).items()):
                print(f"  M{mno}: {home} vs {away}")
        else:
            print("\n(Round of 32 resolves once all 72 group games are final.)")

        if args.generate:
            n = generate_knockout(conn)
            print(f"\ngenerate_knockout: {n} fixtures inserted.")


if __name__ == "__main__":
    main()
