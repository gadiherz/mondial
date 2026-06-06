"""Kickoff-week HDA value picks: model probabilities vs. the WINNER benchmark.

For each scheduled week-1 match it shows the model's P(H/D/A), Winner's de-vigged
("true") P(H/D/A), and the VALUE pick = argmax_o (model_p_o * odd_o) made AGAINST
the Winner 1X2 line -- the eval benchmark the six-player sim bets and settles at
(ARCHITECTURE.md D2/§7). The plain argmax-probability pick is shown alongside to
highlight where they differ (typically the draw).

Requires Winner odds in the DB: run `scripts/fetch_winner_odds.py` first. Matches
with no Winner price yet are listed as such (no silent fallback to another book).

Usage: PYTHONPATH=src python scripts/value_picks.py
"""
from mondial.config import CALIBRATOR_PATH, MIN_PROB_EDGE
from mondial.data.db import connect
from mondial.eval.odds import devig, load_eval_odds
from mondial.eval.simulator import ModelProbs, value_pick
from mondial.model import features as feat_module
from mondial.model.calibration import calibrate_hda, load_calibrator
from mondial.model.dixon_coles import DCParams, predict_match

WC_HOSTS = {"United States", "Mexico", "Canada"}


def _intel_tag(conn, home_id, away_id) -> str:
    """Compact provenance: i=informed, .=no-signal, ?=missing, per home/away."""
    rows = {r["team_id"]: r for r in conn.execute(
        "SELECT team_id, confidence FROM team_intel WHERE team_id IN (?,?)",
        (home_id, away_id))}

    def one(tid):
        r = rows.get(tid)
        if r is None:
            return "?"
        return "i" if (r["confidence"] or 0) >= 0.05 else "."
    return one(home_id) + one(away_id)


def main() -> None:
    params = DCParams.from_json()
    cal = load_calibrator(CALIBRATOR_PATH)
    print("model probs:", "CALIBRATED" if cal is not None
          else "RAW (no calibrator -- run `train --calibrate`)")
    print(f"value guard: min_prob_edge={MIN_PROB_EDGE:g} (a '*' on VALUE = guard "
          "reverted a contrarian flip to the favorite; calibrate via sweep_min_edge.py)")
    print("intel col: 2 chars (home,away) -- i=informed  .=no-signal  ?=missing")
    with connect() as conn:
        rows = conn.execute(
            """SELECT m.match_id, m.date, m.home_id, h.name AS home,
                      m.away_id, a.name AS away
               FROM matches m
               JOIN teams h ON m.home_id=h.team_id
               JOIN teams a ON m.away_id=a.team_id
               WHERE m.status='scheduled'
                 AND m.date BETWEEN '2026-06-11' AND '2026-06-18'
               ORDER BY m.date, m.match_id"""
        ).fetchall()

    hdr = (f"{'Date':<11}{'Match':<40}{'intel':<6}{'model H/D/A':<20}"
           f"{'Winner true H/D/A':<26}{'argmax':<8}{'VALUE':<6}")
    print(hdr); print("-" * len(hdr))
    n_with_odds = 0
    for r in rows:
        neutral = r["home"] not in WC_HOSTS
        with connect() as conn:
            try:
                xh, xa = feat_module.build_feature_vector(
                    r["home_id"], r["away_id"], r["date"], conn)
            except LookupError:
                continue
            p_h, p_d, p_a = predict_match(
                params, r["home_id"], r["away_id"],
                x_home=xh, x_away=xa, neutral=neutral)
            if cal is not None:
                p_h, p_d, p_a = calibrate_hda(cal, p_h, p_d, p_a)
            odds_res = load_eval_odds(conn, r["match_id"])  # Winner = the benchmark
            tag = _intel_tag(conn, r["home_id"], r["away_id"])

        match = f"{r['home']} v {r['away']}"[:39]
        model_str = f"{p_h:.2f}/{p_d:.2f}/{p_a:.2f}"
        argmax = max(zip("HDA", (p_h, p_d, p_a)), key=lambda x: x[1])[0]
        if odds_res is None:
            print(f"{r['date']:<11}{match:<40}{tag:<6}{model_str:<20}"
                  f"{'-- no Winner odds yet --':<26}{argmax:<8}{'-':<6}")
            continue
        n_with_odds += 1
        odds, _ = odds_res
        tp_h, tp_d, tp_a = devig(odds)
        true_str = f"{tp_h:.2f}/{tp_d:.2f}/{tp_a:.2f} (winner)"
        mp = ModelProbs(p_h, p_d, p_a)
        vpick = value_pick(mp, odds, MIN_PROB_EDGE)
        ungated = value_pick(mp, odds, 0.0)
        vstr = vpick + ("*" if vpick != ungated else "")  # * = guard reverted the flip
        print(f"{r['date']:<11}{match:<40}{tag:<6}{model_str:<20}"
              f"{true_str:<26}{argmax:<8}{vstr:<6}")

    print(f"\n{n_with_odds}/{len(rows)} week-1 matches have Winner odds loaded.")
    if n_with_odds == 0:
        print("Run `PYTHONPATH=src python scripts/fetch_winner_odds.py`.")


if __name__ == "__main__":
    main()
