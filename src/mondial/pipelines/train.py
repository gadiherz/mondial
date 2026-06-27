"""Offline training entry point.

Usage:
    python -m mondial.pipelines.train --bootstrap   # download + Elo backfill
    python -m mondial.pipelines.train --fit         # fit Dixon-Coles, persist params

Both flags can be passed in one invocation; bootstrap runs first.
"""
from __future__ import annotations

import argparse
import json
import logging
from datetime import date, datetime, UTC
from typing import Any

import numpy as np
import pandas as pd

from mondial.config import CALIBRATOR_PATH, DC_PARAMS_PATH
from mondial.data.db import connect, init_db
from mondial.eval.metrics import brier_score, log_loss
from mondial.model import elo as elo_module
from mondial.model import features as feat_module
from mondial.model import ratings as ratings_module
from mondial.model.calibration import make_calibrator
from mondial.model.dixon_coles import fit as dc_fit
from mondial.model.dixon_coles import predict_match
from mondial.scrapers.historical_results import HistoricalResultsScraper

log = logging.getLogger("train")

# Final matches joined to their leakage-free PRE-match feature row. Shared by
# the DC fit and the calibration holdout.
_FEATURE_SQL = """
    SELECT m.match_id, m.date, m.neutral, m.home_id, m.away_id,
           m.home_goals, m.away_goals,
           f.home_glicko, f.away_glicko, f.home_rd, f.away_rd,
           f.home_resid_ewma, f.away_resid_ewma, f.home_gd_ewma, f.away_gd_ewma,
           f.home_idle_days, f.away_idle_days
    FROM matches m
    JOIN match_features f ON m.match_id = f.match_id
    WHERE m.status='final' AND m.home_goals IS NOT NULL AND m.away_goals IS NOT NULL
    ORDER BY m.date"""


def _feature_frame(conn) -> pd.DataFrame:
    return pd.read_sql(_FEATURE_SQL, conn)


def bootstrap() -> None:
    """Pull historical matches into SQLite, then backfill Elo end-to-end."""
    init_db()

    scraper = HistoricalResultsScraper()
    log.info("fetching Jurisoo CSV (since %s)", "2014-01-01")
    records = scraper.fetch()
    log.info("got %d records", len(records))

    with connect() as conn:
        scraper.upsert(records, conn)
        n_teams = conn.execute("SELECT COUNT(*) AS n FROM teams").fetchone()["n"]
        n_final = conn.execute(
            "SELECT COUNT(*) AS n FROM matches WHERE status='final'"
        ).fetchone()["n"]
        n_sched = conn.execute(
            "SELECT COUNT(*) AS n FROM matches WHERE status='scheduled'"
        ).fetchone()["n"]
    log.info("teams=%d, matches final=%d scheduled=%d", n_teams, n_final, n_sched)

    _backfill_elo()
    _backfill_glicko()


def _backfill_glicko() -> None:
    """Stream all final matches through the leakage-free Glicko-2 + momentum pass.

    Populates `match_features` (per-match PRE-match state used as training
    features) and `team_state` (latest per-team snapshot used to predict
    scheduled matches). This is the rating engine V1 actually trains on; the Elo
    backfill above is retained only as a legacy point rating in the `ratings`
    table and feeds nothing downstream.
    """
    with connect() as conn:
        written = ratings_module.stream_backfill(conn)
        n_state = conn.execute("SELECT COUNT(*) AS n FROM team_state").fetchone()["n"]
    log.info("Glicko backfill: %d match_features rows, %d team_state rows",
             written, n_state)


def _backfill_elo() -> None:
    """Stream all final matches in date order through elo.update.

    Legacy point rating: superseded by the Glicko-2 engine for features, kept
    only to populate `ratings.elo`. See _backfill_glicko.
    """
    with connect() as conn:
        rows = conn.execute(
            """SELECT date, home_id, away_id, home_goals, away_goals,
                      tournament, neutral
               FROM matches
               WHERE status = 'final'
               ORDER BY date, match_id"""
        ).fetchall()
        team_ids = [
            r["team_id"] for r in conn.execute("SELECT team_id FROM teams").fetchall()
        ]

    ratings: dict[int, float] = {tid: elo_module.DEFAULT_RATING for tid in team_ids}
    for r in rows:
        upd = elo_module.update(
            home=ratings[r["home_id"]],
            away=ratings[r["away_id"]],
            home_goals=int(r["home_goals"]),
            away_goals=int(r["away_goals"]),
            tournament=r["tournament"],
            neutral=bool(r["neutral"]),
        )
        ratings[r["home_id"]] = upd.new_home
        ratings[r["away_id"]] = upd.new_away

    today = date.today().isoformat()
    with connect() as conn:
        for tid, elo in ratings.items():
            conn.execute(
                """INSERT INTO ratings (team_id, date, elo) VALUES (?, ?, ?)
                   ON CONFLICT(team_id, date) DO UPDATE SET elo=excluded.elo""",
                (tid, today, elo),
            )
    top = sorted(ratings.items(), key=lambda kv: kv[1], reverse=True)[:5]
    log.info(
        "Elo backfilled for %d teams; top 5: %s",
        len(ratings),
        [(tid, round(r, 1)) for tid, r in top],
    )


MIN_TEAM_MATCHES = 20  # drop teams with < N final matches before fitting


def _filter_thin_teams(df: pd.DataFrame, min_matches: int) -> pd.DataFrame:
    """Keep only matches where BOTH teams have >= min_matches appearances.

    Without this the long tail of minor sides (Island Games squads, etc.)
    produces extreme alpha/beta estimates from a few-match sample. We want to
    model international football at scale, not unstable 3-match outliers.
    """
    counts = pd.concat([df["home_id"], df["away_id"]]).value_counts()
    keep = set(counts[counts >= min_matches].index)
    return df[df["home_id"].isin(keep) & df["away_id"].isin(keep)].copy()


DC_L2 = 1e-3  # ridge penalty on the Glicko/momentum feature weights


def fit() -> None:
    """Fit Dixon-Coles on all final matches; persist DCParams to JSON.

    Each match is joined to its leakage-free PRE-match state in `match_features`
    (written by _backfill_glicko) and turned into the V1 Glicko/momentum feature
    matrices by features.design_matrices. The DC parameters alpha, beta, mu,
    home_adv, rho AND the feature weights w are learned jointly.
    """
    with connect() as conn:
        n_final = conn.execute(
            """SELECT COUNT(*) AS n FROM matches
               WHERE status='final' AND home_goals IS NOT NULL
                 AND away_goals IS NOT NULL"""
        ).fetchone()["n"]
        # INNER JOIN: only matches that have a PRE-match feature row can be fit.
        df = _feature_frame(conn)
    if df.empty:
        raise RuntimeError(
            "No final matches with feature rows in DB. Run "
            "`python -m mondial.pipelines.train --bootstrap` first "
            "(it now also runs the Glicko backfill that fills match_features)."
        )
    if len(df) < n_final:
        # Safety: never silently train on a subset because the backfill is stale.
        log.warning(
            "match_features covers only %d of %d final matches (%d missing). "
            "Re-run --bootstrap so the Glicko backfill is up to date.",
            len(df), n_final, n_final - len(df),
        )
    n_before = len(df)
    df = _filter_thin_teams(df, MIN_TEAM_MATCHES)
    n_teams = df[["home_id", "away_id"]].stack().nunique()
    log.info(
        "fitting DC on %d matches (%d filtered out for thin-team threshold "
        "%d), %d teams, %d features %s",
        len(df), n_before - len(df), MIN_TEAM_MATCHES, n_teams,
        feat_module.n_features(), feat_module.feature_names(),
    )

    # Feature matrices, aligned row-for-row with the (filtered, date-ordered) df.
    X_home, X_away = feat_module.design_matrices(df)
    params = dc_fit(df, X_home, X_away, l2=DC_L2, maxiter=2000)

    DC_PARAMS_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "mu": params.mu,
        "home_adv": params.home_adv,
        "rho": params.rho,
        "feature_names": feat_module.feature_names(),
        "feature_weights": params.feature_weights.tolist(),
        "attack": params.attack,
        "defence": params.defence,
        "fit_at": datetime.now(UTC).isoformat(),
        "n_matches": int(len(df)),
        "n_teams": len(params.attack),
    }
    DC_PARAMS_PATH.write_text(json.dumps(payload, indent=2))
    log.info("DC params written to %s", DC_PARAMS_PATH)

    today = date.today().isoformat()
    with connect() as conn:
        for tid in params.attack:
            conn.execute(
                """INSERT INTO ratings (team_id, date, dc_attack, dc_defence)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(team_id, date) DO UPDATE SET
                       dc_attack=excluded.dc_attack,
                       dc_defence=excluded.dc_defence""",
                (tid, today, params.attack[tid], params.defence[tid]),
            )

    # Sanity-check report.
    top_atk = sorted(params.attack.items(), key=lambda kv: kv[1], reverse=True)[:5]
    bot_def = sorted(params.defence.items(), key=lambda kv: kv[1])[:5]
    with connect() as conn:
        names = {r["team_id"]: r["name"] for r in conn.execute("SELECT team_id, name FROM teams")}
    log.info("top-5 attack (alpha):  %s", [(names[t], round(v, 3)) for t, v in top_atk])
    log.info("top-5 defence (low beta = best): %s",
             [(names[t], round(v, 3)) for t, v in bot_def])
    log.info("mu=%.3f  home_adv=%.3f  rho=%.3f",
             params.mu, params.home_adv, params.rho)
    log.info("feature weights: %s", dict(zip(
        feat_module.feature_names(),
        [round(float(w), 4) for w in params.feature_weights])))


HOLDOUT_FRAC = 0.15  # most-recent time slice held out to fit calibration

# Stage-B calibrator. 'temperature_draw' (temperature + a draw-class log-bias, 2
# parameters) is the V1 choice. Plain single-parameter 'temperature' (Guo et al.
# 2017) keeps the model well-calibrated overall, but the fitted T<1 SHARPENS and so
# demotes the draw class (rarely the argmax) -- the model then almost never picks a
# draw despite a ~25-30% real draw rate. The one extra draw-bias parameter restores
# the draw to its honest frequency while keeping H/A sharpness, and -- being just one
# DoF beyond temperature -- keeps the anti-overfit property that ruled out the
# flexible per-class 'isotonic' (which overfit its holdout and broke the WC-2022
# backtest). The backtest (eval/backtest.py) mirrors this exactly.
CALIBRATION_METHOD = "temperature_draw"


def calibrate() -> None:
    """Fit the Stage-B calibrator on a held-out, most-recent time slice.

    Refits DC on the earlier 85% (so holdout predictions are genuinely
    out-of-sample), predicts the holdout, fits the Stage-B calibrator
    (CALIBRATION_METHOD, default TemperatureScaler) on (raw prob, outcome),
    reports raw vs. calibrated log-loss/Brier, and persists the calibrator.
    Production prediction uses the full-data DC params with this calibrator on
    top -- the standard fit-on-holdout / apply-to-final pattern.
    """
    with connect() as conn:
        df = _feature_frame(conn)
    if df.empty:
        raise RuntimeError("No feature rows. Run --bootstrap then --fit first.")
    df = _filter_thin_teams(df, MIN_TEAM_MATCHES).sort_values("date").reset_index(drop=True)

    cut = int((1.0 - HOLDOUT_FRAC) * len(df))
    train_df, hold_df = df.iloc[:cut], df.iloc[cut:].reset_index(drop=True)
    log.info("calibration split: train=%d (<= %s), holdout=%d (%s .. %s)",
             len(train_df), train_df["date"].iloc[-1], len(hold_df),
             hold_df["date"].iloc[0], hold_df["date"].iloc[-1])

    Xh_tr, Xa_tr = feat_module.design_matrices(train_df)
    params = dc_fit(train_df, Xh_tr, Xa_tr, l2=DC_L2, maxiter=2000)

    Xh_h, Xa_h = feat_module.design_matrices(hold_df)
    home = hold_df["home_id"].to_numpy()
    away = hold_df["away_id"].to_numpy()
    neutral = hold_df["neutral"].to_numpy()
    hg = hold_df["home_goals"].to_numpy()
    ag = hold_df["away_goals"].to_numpy()

    probs, outs, skipped = [], [], 0
    for i in range(len(hold_df)):
        try:
            p = predict_match(params, int(home[i]), int(away[i]),
                              x_home=Xh_h[i], x_away=Xa_h[i], neutral=bool(neutral[i]))
        except KeyError:
            skipped += 1  # team debuts in the holdout window -> not in train fit
            continue
        probs.append(p)
        outs.append(0 if hg[i] > ag[i] else 1 if hg[i] == ag[i] else 2)
    if skipped:
        log.warning("calibration: skipped %d holdout matches with a team absent "
                    "from the train slice", skipped)
    probs = np.array(probs)
    outs = np.array(outs)

    cal = make_calibrator(CALIBRATION_METHOD).fit(probs, outs)
    cal_probs = cal.transform(probs)
    # Draw-frequency calibration check: mean predicted P(D) should track the
    # empirical draw rate on the holdout (the whole point of the draw-bias param).
    draw_rate = float(np.mean(outs == 1))
    log.info("holdout RAW : log-loss=%.4f  brier=%.4f  meanP(D)=%.3f",
             log_loss(probs, outs), brier_score(probs, outs), float(probs[:, 1].mean()))
    extra = ""
    if hasattr(cal, "temperature"):
        extra = f", T={cal.temperature:.3f}"
    if hasattr(cal, "draw_bias"):
        extra += f", draw_bias={cal.draw_bias:.3f}"
    log.info("holdout CAL : log-loss=%.4f  brier=%.4f  meanP(D)=%.3f  "
             "(empirical draw rate=%.3f; method=%s%s)",
             log_loss(cal_probs, outs), brier_score(cal_probs, outs),
             float(cal_probs[:, 1].mean()), draw_rate, CALIBRATION_METHOD, extra)
    cal.save(CALIBRATOR_PATH)
    log.info("calibrator written to %s", CALIBRATOR_PATH)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser()
    parser.add_argument("--bootstrap", action="store_true")
    parser.add_argument("--fit", action="store_true")
    parser.add_argument("--calibrate", action="store_true")
    args = parser.parse_args()

    if not (args.bootstrap or args.fit or args.calibrate):
        parser.error("Specify --bootstrap and/or --fit and/or --calibrate")

    if args.bootstrap:
        bootstrap()
    if args.fit:
        fit()
    if args.calibrate:
        calibrate()


if __name__ == "__main__":
    main()
