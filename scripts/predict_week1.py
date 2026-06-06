"""Demonstrate week-1 WC 2026 predictions from the fitted DCParams.

Not part of the package. This is a one-shot illustration of what
predict_upcoming() in daily_run.py should eventually produce.
"""
from mondial.data.db import connect
from mondial.model import features as feat_module
from mondial.model.dixon_coles import DCParams, predict_match

WC_HOSTS = {"United States", "Mexico", "Canada"}


def main() -> None:
    params = DCParams.from_json()
    with connect() as conn:
        rows = conn.execute(
            """SELECT m.date, m.home_id, h.name AS home, m.away_id, a.name AS away
               FROM matches m
               JOIN teams h ON m.home_id=h.team_id
               JOIN teams a ON m.away_id=a.team_id
               WHERE m.status='scheduled'
                 AND m.date BETWEEN '2026-06-11' AND '2026-06-18'
               ORDER BY m.date, m.match_id"""
        ).fetchall()

    print(f"{'Date':<12} {'Home':<22} {'Away':<22}  {'P(H)':>6} {'P(D)':>6} {'P(A)':>6}  Pick")
    print("-" * 90)
    for r in rows:
        # WC matches are neutral except when a host plays at "home".
        is_neutral = r["home"] not in WC_HOSTS
        try:
            with connect() as conn:
                x_home, x_away = feat_module.build_feature_vector(
                    r["home_id"], r["away_id"], r["date"], conn
                )
            p_h, p_d, p_a = predict_match(
                params, r["home_id"], r["away_id"],
                x_home=x_home, x_away=x_away, neutral=is_neutral,
            )
        except KeyError as e:
            # Team filtered out during fit (thin-team threshold).
            print(f"{r['date']:<12} {r['home']:<22} {r['away']:<22}  ----- skip: team {e} not in fit")
            continue
        except LookupError as e:
            # No team_state row (Glicko backfill not run for this side).
            print(f"{r['date']:<12} {r['home']:<22} {r['away']:<22}  ----- skip: {e}")
            continue
        pick = max(zip("HDA", (p_h, p_d, p_a)), key=lambda x: x[1])[0]
        print(
            f"{r['date']:<12} {r['home']:<22} {r['away']:<22}  "
            f"{p_h:6.3f} {p_d:6.3f} {p_a:6.3f}  {pick}"
        )


if __name__ == "__main__":
    main()
