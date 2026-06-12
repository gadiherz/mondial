"""Shared orchestration step library + a combined local entry point.

In PRODUCTION the work is split across two GitHub Actions routines that import
these steps: Routine A `pipelines/markets_intel.py` (.github/workflows/markets-
intel.yml -- odds + play-day intel + predict + publish) and Routine B
`pipelines/results.py` (.github/workflows/results.yml -- ingest results +
re-backfill ratings). This module's `main()` runs the whole sequence in one shot
for local/manual use; the old single `daily.yml` it once backed was removed.

Pipeline (each step is failure-isolated via @step):
  1. scrape_all              -- pull live HDA odds (The Odds API).
  2. update_ratings          -- re-run the leakage-free Glicko-2 + momentum
                                backfill so team_state reflects any newly-final
                                results (this is what makes round-2 features pick
                                up round-1 outcomes).
  3. predict_upcoming        -- model P(H/D/A) for scheduled matches, persisted
                                to `predictions`, plus a value pick vs. live odds.

The six-player betting simulator (settle_bets / place_virtual_bets) is the
front-end evaluation layer and is intentionally left out here; see eval/.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import UTC, datetime

from mondial.config import CALIBRATOR_PATH
from mondial.data.db import connect, init_db
from mondial.eval.betting import place_bets, settle_bets
from mondial.eval.odds import devig, load_eval_odds, load_match_odds
from mondial.eval.simulator import ModelProbs, value_pick
from mondial.intel.collect import collect_intel
from mondial.model import features as feat_module
from mondial.model import glicko
from mondial.model import ratings as ratings_module
from mondial.model.calibration import calibrate_hda, load_calibrator
from mondial.model.dixon_coles import DCParams, predict_match, predict_match_breakdown
from mondial.scrapers.odds_api import OddsAPIScraper
from mondial.scrapers.winner_odds import WinnerOddsScraper

log = logging.getLogger("daily_run")


def step(name: str):
    """Per-source failure isolation: log and continue. See ARCHITECTURE.md §4.3."""
    def wrap(fn):
        def inner(*a, **kw):
            try:
                return fn(*a, **kw)
            except Exception as e:  # noqa: BLE001
                log.exception("step %s failed: %s", name, e)
                return None
        return inner
    return wrap


@step("scrape")
def scrape_all() -> None:
    """Run all scrapers (each with its own error isolation).

    Only the odds scraper is wired today; FIFA-rank / squad-value / prediction-
    market scrapers remain stubs and are skipped with a note.
    """
    scraper = OddsAPIScraper()
    records = scraper.fetch()
    with connect() as conn:
        scraper.upsert(records, conn)
    log.info("scrape_all: odds fetched for %d events; other scrapers not wired",
             len(records))


@step("winner")
def scrape_winner() -> None:
    """Fetch Winner (Israeli TOTO) 1X2 odds via the bankerim aggregator.

    The WINNER-16 eval reference (ARCHITECTURE D2), written to odds_snapshots as
    bookmaker='winner'. Failure-isolated: a layout change on the third-party
    aggregator must never break the prediction pipeline. See scrapers/winner_odds.
    """
    scraper = WinnerOddsScraper()
    records = scraper.fetch()
    with connect() as conn:
        scraper.upsert(records, conn)
    log.info("scrape_winner: %d Winner football matches parsed", len(records))


@step("intel")
def refresh_team_intel(within_days: int | None = None) -> None:
    """Autonomous LLM intel logger (Track B) -- refresh team_intel from APIs.

    `within_days` scopes to teams playing in that window (Routine A passes the
    play-day window). Failure-isolated: if a provider/key is unavailable, this
    logs and the pipeline continues with whatever intel is already stored.
    """
    with connect() as conn:
        collect_intel(conn, within_days=within_days)


@step("update_ratings")
def update_ratings_for_completed() -> None:
    """Re-derive Glicko-2 + momentum from all final matches.

    A full idempotent recompute (cheap at ~12k matches). Once a round's results
    are marked status='final', this refreshes team_state so the next round's
    features reflect them.
    """
    with connect() as conn:
        written = ratings_module.stream_backfill(conn)
    log.info("update_ratings: refreshed match_features/team_state (%d rows)", written)


def _neutral(row) -> bool:
    """Prefer the DB's neutral flag (WC venues are neutral except host games)."""
    return bool(row["neutral"])


def _intel_status(row) -> dict:
    """Per-team intel provenance for the frontend to badge a prediction.

      missing   -- no intel row (failed / deferred / not yet fetched): this side
                   of the prediction used RATINGS ONLY, no qualitative info.
      no_signal -- processed, but the sources held no concrete info (conf ~ 0):
                   no contribution.
      informed  -- a real qualitative signal was applied.
    """
    if row is None:
        return {"status": "missing", "composite": None, "confidence": None}
    comp, conf = row["composite"], row["confidence"]
    status = "no_signal" if (conf is None or conf < 0.05) else "informed"
    return {
        "status": status,
        "composite": round(comp, 4) if comp is not None else None,
        "confidence": round(conf, 4) if conf is not None else None,
    }


def _build_breakdown(conn, params, cal, row, xh, xa, raw_probs, cal_probs, intel_rows) -> dict:
    """Assemble one match's 'why this prediction' payload (JSON-serialisable).

    Combines the Dixon-Coles decomposition (lambdas, score grid, per-feature
    contributions) with the Glicko/momentum inputs, the LLM-intel read, and the
    raw-vs-calibrated probabilities. Computed from the SAME x_home/x_away that
    produced the stored prediction, so the explanation can never disagree with it.
    """
    home_id, away_id, mdate = row["home_id"], row["away_id"], row["date"]
    neutral = _neutral(row)
    dc = predict_match_breakdown(
        params, home_id, away_id, x_home=xh, x_away=xa,
        neutral=neutral, feature_names=feat_module.feature_names())
    desc = {s.name: s.description for s in feat_module.SPEC}
    for f in dc["features"]:
        f["description"] = desc.get(f["name"], "")

    fs = feat_module.feature_state_breakdown(home_id, away_id, mdate, conn)
    home_rating = glicko.Rating(fs["home"]["glicko"], fs["home"]["rd"], fs["home"]["vol"])
    away_rating = glicko.Rating(fs["away"]["glicko"], fs["away"]["rd"], fs["away"]["vol"])
    home_expected = float(glicko.expected_score(home_rating, away_rating, neutral=neutral))

    def intel_block(team_id: int) -> dict:
        st = _intel_status(intel_rows.get(team_id))
        r = intel_rows.get(team_id)
        dims = summary = None
        if r is not None:
            dims = {k: (round(r[k], 3) if r[k] is not None else None) for k in
                    ("form_outlook", "availability", "coach_stability", "morale", "motivation")}
            summary = r["summary"]
        return {"status": st["status"], "composite": st["composite"],
                "confidence": st["confidence"], "summary": summary, "dims": dims}

    return {
        **dc,
        "glicko": {
            "home": {"rating": round(fs["home"]["glicko"]), "rd": round(fs["home"]["rd"]),
                     "intel_points": round(fs["home"]["intel_delta"], 1)},
            "away": {"rating": round(fs["away"]["glicko"]), "rd": round(fs["away"]["rd"]),
                     "intel_points": round(fs["away"]["intel_delta"], 1)},
            "home_expected": round(home_expected, 3),
        },
        "momentum": {
            "home": {"resid_ewma": round(fs["home"]["resid_ewma"], 3),
                     "gd_ewma": round(fs["home"]["gd_ewma"], 2),
                     "idle_days": round(fs["home"]["idle_days"])},
            "away": {"resid_ewma": round(fs["away"]["resid_ewma"], 3),
                     "gd_ewma": round(fs["away"]["gd_ewma"], 2),
                     "idle_days": round(fs["away"]["idle_days"])},
        },
        "intel": {"home": intel_block(home_id), "away": intel_block(away_id)},
        "calibration": {
            "temperature": (round(cal.temperature, 4)
                            if cal is not None and hasattr(cal, "temperature") else None),
            "raw": [round(p, 4) for p in raw_probs],
            "calibrated": [round(p, 4) for p in cal_probs],
        },
    }


@step("predict")
def predict_upcoming() -> list[dict]:
    """Predict all scheduled matches; persist to `predictions`; return snapshot rows.

    Each row carries the model's P(H/D/A); when a sharp book exists, its de-vigged
    "true" probabilities (the model-vs-market edge reference); and when Winner odds
    exist, the Winner 1X2 line plus the value pick made AGAINST Winner -- the eval
    benchmark the six-player sim bets and settles at (ARCHITECTURE D2). Either odds
    source may be absent independently; value_pick is null with no Winner price (no
    silent fallback to a different book).
    """
    params = DCParams.from_json()
    cal = load_calibrator(CALIBRATOR_PATH)
    if cal is None:
        log.warning("predict: no calibrator at %s -- using RAW probabilities. "
                    "Run `train --calibrate`.", CALIBRATOR_PATH)
    predicted_at = datetime.now(UTC).isoformat()
    snapshot: list[dict] = []

    with connect() as conn:
        intel = {r["team_id"]: r for r in conn.execute(
            "SELECT team_id, composite, confidence, summary, form_outlook, "
            "availability, coach_stability, morale, motivation FROM team_intel")}
        if not intel:
            log.warning("predict: no team_intel rows -- EVERY prediction uses "
                        "ratings only (run `scripts/fetch_intel.py`).")
        else:
            informed = sum(1 for r in intel.values()
                           if r["confidence"] is not None and r["confidence"] >= 0.05)
            log.info("predict: intel present for %d teams (%d informed, %d no-signal)",
                     len(intel), informed, len(intel) - informed)
        rows = conn.execute(
            """SELECT m.match_id, m.date, m.neutral,
                      m.home_id, h.name AS home, m.away_id, a.name AS away
               FROM matches m
               JOIN teams h ON m.home_id=h.team_id
               JOIN teams a ON m.away_id=a.team_id
               WHERE m.status='scheduled'
               ORDER BY m.date, m.match_id"""
        ).fetchall()

        for r in rows:
            try:
                xh, xa = feat_module.build_feature_vector(
                    r["home_id"], r["away_id"], r["date"], conn)
            except LookupError as e:
                log.warning("predict: skipping %s v %s -- %s", r["home"], r["away"], e)
                continue
            try:
                p_h, p_d, p_a = predict_match(
                    params, r["home_id"], r["away_id"],
                    x_home=xh, x_away=xa, neutral=_neutral(r))
            except KeyError as e:
                log.warning("predict: skipping %s v %s -- team %s not in fit",
                            r["home"], r["away"], e)
                continue
            raw_probs = (p_h, p_d, p_a)  # pre-calibration, for the breakdown panel
            if cal is not None:
                p_h, p_d, p_a = calibrate_hda(cal, p_h, p_d, p_a)

            conn.execute(
                """INSERT INTO predictions (match_id, p_home, p_draw, p_away, predicted_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(match_id) DO UPDATE SET
                       p_home=excluded.p_home, p_draw=excluded.p_draw,
                       p_away=excluded.p_away, predicted_at=excluded.predicted_at""",
                (r["match_id"], p_h, p_d, p_a, predicted_at),
            )

            # Persist the per-match explanation alongside the prediction. Failure-
            # isolated: a breakdown error must never lose the prediction itself.
            try:
                bd = _build_breakdown(conn, params, cal, r, xh, xa,
                                      raw_probs, (p_h, p_d, p_a), intel)
                conn.execute(
                    """INSERT INTO match_breakdown (match_id, json, computed_at)
                       VALUES (?, ?, ?)
                       ON CONFLICT(match_id) DO UPDATE SET
                           json=excluded.json, computed_at=excluded.computed_at""",
                    (r["match_id"], json.dumps(bd), predicted_at),
                )
            except Exception as e:  # noqa: BLE001
                log.warning("predict: breakdown skipped for %s v %s (%s)",
                            r["home"], r["away"], e)

            row = {
                "match_id": r["match_id"], "date": r["date"],
                "home": r["home"], "away": r["away"],
                "p_home": round(p_h, 4), "p_draw": round(p_d, 4), "p_away": round(p_a, 4),
                # sharp (Pinnacle) de-vigged "true" market probs -- edge reference
                "book": None, "true_home": None, "true_draw": None, "true_away": None,
                # Winner 1X2 -- the eval benchmark the value pick/settlement use
                "winner_home": None, "winner_draw": None, "winner_away": None,
                "value_pick": None, "value_vs": None,
            }
            hs = _intel_status(intel.get(r["home_id"]))
            as_ = _intel_status(intel.get(r["away_id"]))
            row.update(
                # Explicit provenance: did THIS prediction use qualitative info?
                qualitative_used=bool(abs(hs["composite"] or 0.0) > 1e-9
                                      or abs(as_["composite"] or 0.0) > 1e-9),
                intel_home_status=hs["status"], intel_home=hs["composite"],
                intel_home_conf=hs["confidence"],
                intel_away_status=as_["status"], intel_away=as_["composite"],
                intel_away_conf=as_["confidence"],
            )
            # Sharp book (Pinnacle-first) -> de-vigged "true" market probabilities,
            # shown for the model-vs-market edge display (NOT what we bet against).
            sharp = load_match_odds(conn, r["match_id"])
            if sharp is not None:
                odds, book = sharp
                tp_h, tp_d, tp_a = devig(odds)
                row.update(book=book, true_home=round(tp_h, 4),
                           true_draw=round(tp_d, 4), true_away=round(tp_a, 4))
            # Winner is the eval benchmark: the value pick is made against the price
            # the six-player sim actually bets and settles at (ARCHITECTURE D2/§7).
            winner = load_eval_odds(conn, r["match_id"])
            if winner is not None:
                wodds, _ = winner
                row.update(
                    winner_home=wodds.odd_home, winner_draw=wodds.odd_draw,
                    winner_away=wodds.odd_away, value_vs="winner",
                    value_pick=value_pick(ModelProbs(p_h, p_d, p_a), wodds),
                )
            snapshot.append(row)

    n_priced = sum(1 for s in snapshot if s["value_pick"])
    log.info("predict_upcoming: %d predictions persisted, %d with Winner odds "
             "(value picks vs the WINNER benchmark)", len(snapshot), n_priced)
    return snapshot


@step("place_bets")
def place_virtual_bets() -> None:
    """Place the six-player virtual bets for priced upcoming matches (Routine A).

    Runs after predict + Winner-odds scrape so both inputs exist. Idempotent; see
    eval/betting.place_bets.
    """
    with connect() as conn:
        place_bets(conn)


@step("settle_bets")
def settle_virtual_bets() -> None:
    """Settle six-player bets whose matches are now final (Routine B)."""
    with connect() as conn:
        settle_bets(conn)


@step("publish")
def publish_web() -> None:
    """Refresh the static-site data feed (web/data/dashboard.json) so the deployed
    site updates after every run. Public match data only; see pipelines/export_web."""
    from mondial.pipelines.export_web import export
    export()


@step("bracket")
def generate_bracket() -> None:
    """Generate WC knockout fixtures from completed results (Routine B).

    A no-op until the group stage finishes; then it creates the R32 and, as each
    knockout round finalises, the next round. New fixtures become prediction +
    betting targets on the next Routine A pass. See model/bracket.generate_knockout.
    """
    from mondial.model.bracket import generate_knockout
    with connect() as conn:
        generate_knockout(conn)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true")
    parser.parse_args()

    init_db()
    scrape_all()
    scrape_winner()
    refresh_team_intel()
    update_ratings_for_completed()
    predictions = predict_upcoming() or []
    place_virtual_bets()      # place six-player bets at the Winner line
    settle_virtual_bets()     # settle any bets whose matches are now final
    generate_bracket()        # create knockout fixtures once group stage is done
    publish_web()             # refresh the static-site data feed
    log.info("daily_run complete: %d predictions; web feed published", len(predictions))
    return 0


if __name__ == "__main__":
    sys.exit(main())
