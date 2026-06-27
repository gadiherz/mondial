"""Backtest harness -- re-runs the model against completed historical tournaments.

Two layers (see ARCHITECTURE.md §7.3):

  LAYER 1 -- predictive skill & calibration (NO odds needed).
    Refit Dixon-Coles on every final match strictly BEFORE the tournament's first
    day (so no tournament goal leaks into the attack/defence/feature fit), then
    predict each tournament match using the leakage-free Glicko/momentum features
    already recorded in `match_features` (those online-update through the
    tournament but depend only on earlier matches -- the same online-rating
    behaviour production uses). Report log-loss, Brier, accuracy and a pooled
    reliability/ECE table, raw and calibrated, against a uniform baseline.

  LAYER 2 -- ROI / "did we beat the market" (REQUIRES contemporaneous odds).
    Runs the 6-player betting simulation (eval/simulator.py) on the tournament
    using historical closing odds in `odds_snapshots`, scored against the
    consensus line (see _load_odds). WC 2022 odds are loaded for free from the
    football-data.co.uk World Cup workbook (scrapers/historical_odds.py); Euro
    2024 / Copa 2024 are not on football-data, so until a CSV is dropped for them
    this layer is SKIPPED for those with a loud notice (open decision O2, free
    path). The hook auto-activates per-tournament once its odds are loaded.

Faithfulness: this reuses the exact production pieces -- the same feature SQL,
thin-team filter, L2 and the same calibrate-on-holdout pattern from
mondial.pipelines.train -- so a backtest result reflects what production would
have predicted, not a parallel reimplementation.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from mondial.data.db import connect
from mondial.eval import simulator as sim
from mondial.eval.metrics import brier_score, log_loss, reliability_bins, roi
from mondial.model.calibration import make_calibrator
from mondial.model.dixon_coles import fit as dc_fit
from mondial.model.dixon_coles import predict_match
from mondial.model import features as feat_module
from mondial.pipelines import train  # reuse production feature SQL / filters / consts

log = logging.getLogger("backtest")

# Uniform 1/X/2 reference: the no-skill floor any model must clear.
UNIFORM_LOG_LOSS = float(-np.log(1.0 / 3.0))        # 1.0986
UNIFORM_BRIER = 6.0 / 9.0                            # 0.6667 (constant per match)

# Tournament finals registry: key -> (label, tournament_tag, first_day, last_day).
# Dates are the real finals windows in mondial.db (verified against the data).
# WC 2018 + WC 2022 both have FREE consensus odds (football-data.co.uk World Cup
# workbook, sheets WorldCup2018/WorldCup2022) and >4k pre-tournament training
# finals each, so they carry the cross-tournament min-edge sweep. WC 2014 is NOT
# registered: the training corpus only starts 2014-01-01, leaving ~5 months of
# history before it (no team clears the thin-team threshold), so its DC fit is
# degenerate. Euro 2024 / Copa 2024 stay registered for the skill layer but have
# no free contemporaneous odds (not on football-data; Kaggle is auth-walled), so
# their ROI/sweep rows SKIP until a CSV is dropped via ingest_historical_odds --csv.
TOURNAMENTS: dict[str, tuple[str, str, str, str]] = {
    "wc2018":   ("WC 2018",   "WC",   "2018-06-14", "2018-07-15"),
    "wc2022":   ("WC 2022",   "WC",   "2022-11-20", "2022-12-18"),
    "euro2024": ("Euro 2024", "EURO", "2024-06-14", "2024-07-14"),
    "copa2024": ("Copa 2024", "COPA", "2024-06-20", "2024-07-14"),
}


@dataclass
class BacktestResult:
    tournament: str
    n_matches: int            # matches actually scored
    n_skipped: int            # tournament matches dropped (a team absent from the fit)
    # Predictive skill (calibrated = production-equivalent output).
    log_loss_raw: float
    log_loss_cal: float
    brier_raw: float
    brier_cal: float
    accuracy: float           # argmax(calibrated) == actual
    ece_cal: float            # pooled expected calibration error (calibrated)
    reliability: list[dict] = field(default_factory=list)  # per-bin pooled table
    # Market layer (only populated when historical odds exist for the slice).
    roi_by_player: dict[str, float] | None = None
    odds_note: str = ""

    @property
    def beats_uniform(self) -> bool:
        """LAYER-1 acceptance gate: calibrated log-loss beats the no-skill floor."""
        return self.log_loss_cal < UNIFORM_LOG_LOSS

    def passes_gates(self) -> bool:
        """Pre-kickoff gate. Layer 1 (skill) is required; Layer 2 (ROI>0) is only
        asserted when odds were available -- otherwise it cannot be evaluated."""
        if not self.beats_uniform:
            return False
        if self.roi_by_player is not None:
            return self.roi_by_player.get("model_full", -1.0) > 0.0
        return True  # skill gate passed; ROI gate deferred (no historical odds)


# --------------------------------------------------------------------------- #
# Internals
# --------------------------------------------------------------------------- #

def _fit_slice(df: pd.DataFrame, *, maxiter: int = 2000):
    """Fit DC on a (already date-filtered) frame using production filter + L2."""
    sl = train._filter_thin_teams(df, train.MIN_TEAM_MATCHES)
    Xh, Xa = feat_module.design_matrices(sl)
    return dc_fit(sl, Xh, Xa, l2=train.DC_L2, maxiter=maxiter)


def _fit_calibrator(train_df: pd.DataFrame, *, method: str = "temperature_draw"):
    """Production calibrate-on-holdout: refit DC on the earlier 85% of the
    train slice, predict the most-recent 15%, fit the calibrator on it.

    `method` selects the Stage-B calibrator ('temperature' | 'isotonic') and must
    match what `pipelines.train.calibrate` persists, so the backtest stays
    production-faithful. Returns None if the holdout is too small to be meaningful.
    """
    df = train._filter_thin_teams(train_df, train.MIN_TEAM_MATCHES).sort_values(
        "date").reset_index(drop=True)
    cut = int((1.0 - train.HOLDOUT_FRAC) * len(df))
    tr, hold = df.iloc[:cut], df.iloc[cut:].reset_index(drop=True)
    if len(hold) < 200:
        log.warning("calibrator holdout only %d rows; skipping calibration", len(hold))
        return None
    params = _fit_slice(tr)
    Xh, Xa = feat_module.design_matrices(hold)
    home = hold["home_id"].to_numpy(); away = hold["away_id"].to_numpy()
    neu = hold["neutral"].to_numpy(); hg = hold["home_goals"].to_numpy(); ag = hold["away_goals"].to_numpy()
    probs, outs = [], []
    for i in range(len(hold)):
        try:
            p = predict_match(params, int(home[i]), int(away[i]),
                              x_home=Xh[i], x_away=Xa[i], neutral=bool(neu[i]))
        except KeyError:
            continue
        probs.append(p)
        outs.append(0 if hg[i] > ag[i] else 1 if hg[i] == ag[i] else 2)
    return make_calibrator(method).fit(np.array(probs), np.array(outs))


def _outcome(hg: int, ag: int) -> int:
    return 0 if hg > ag else 1 if hg == ag else 2


def _pooled_reliability(probs: np.ndarray, outs: np.ndarray, n_bins: int = 10):
    """Stack all three class columns into one reliability table + ECE."""
    pooled_p = np.concatenate([probs[:, k] for k in range(3)])
    pooled_y = np.concatenate([(outs == k).astype(float) for k in range(3)])
    centers, pred, emp, cnt = reliability_bins(pooled_p, pooled_y, n_bins)
    n = cnt.sum()
    ece = float(np.sum(cnt / n * np.abs(pred - emp))) if n else 0.0
    table = [
        {"bin": f"{centers[b]-0.05:.1f}-{centers[b]+0.05:.1f}",
         "pred": round(float(pred[b]), 3), "emp": round(float(emp[b]), 3),
         "n": int(cnt[b])}
        for b in range(n_bins) if cnt[b]
    ]
    return ece, table


def _load_odds(conn, match_ids: list[int]) -> dict[int, sim.MatchOdds]:
    """Per-match 1X2 odds for the sim (empty if none stored).

    Scores against the CONSENSUS closing line: if a market-average bookmaker is
    present (tag ending '_avg', e.g. football-data's `fd_avg`), use it alone --
    that's the honest "beat the market" reference. Otherwise (live books with no
    explicit average) fall back to the median across books. Best-price tags
    (e.g. `fd_max`) are intentionally NOT favoured: settling at best price would
    optimistically inflate ROI.

    Note: this HISTORICAL backtest necessarily settles at the consensus line --
    Winner has no archived odds, so there is no `winner` row for 2018/2022. The
    LIVE WC-2026 eval settles at the Winner benchmark instead (eval/odds.
    EVAL_BOOKMAKER, used by daily_run/value_picks); the two are different by data
    availability, not by choice.
    """
    if not match_ids:
        return {}
    qs = ",".join("?" * len(match_ids))
    rows = conn.execute(
        f"""SELECT match_id, bookmaker, odd_home, odd_draw, odd_away
            FROM odds_snapshots WHERE match_id IN ({qs})
            AND odd_home IS NOT NULL AND odd_draw IS NOT NULL AND odd_away IS NOT NULL""",
        match_ids,
    ).fetchall()
    by_match: dict[int, list] = {}
    avg_by_match: dict[int, tuple] = {}
    for r in rows:
        triple = (r["odd_home"], r["odd_draw"], r["odd_away"])
        by_match.setdefault(r["match_id"], []).append(triple)
        if str(r["bookmaker"]).endswith("_avg"):
            avg_by_match[r["match_id"]] = triple
    out = {}
    for mid, lst in by_match.items():
        if mid in avg_by_match:
            out[mid] = sim.MatchOdds(*[float(v) for v in avg_by_match[mid]])
        else:
            arr = np.array(lst, dtype=float)
            out[mid] = sim.MatchOdds(*[float(np.median(arr[:, c])) for c in range(3)])
    return out


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #

@dataclass
class TournamentPredictions:
    """Production-faithful per-match predictions for one tournament slice."""
    label: str
    raw_probs: np.ndarray     # (n,3) Dixon-Coles output
    cal_probs: np.ndarray     # (n,3) after Stage-B isotonic (== raw if calibrate=False)
    outs: np.ndarray          # (n,) actual outcomes in {0,1,2}
    kept_ids: list[int]       # match_ids scored (teams present in the pre-cut fit)
    n_skipped: int
    n_total: int


def predict_tournament(conn, key: str, *, calibrate: bool = True,
                       calib_method: str = "temperature_draw") -> TournamentPredictions:
    """Refit DC strictly pre-tournament and predict every tournament match.

    Shared by run_backtest (skill + ROI report) and scripts/sweep_min_edge.py (the
    min-edge sweep) so both score the IDENTICAL probabilities, and so the expensive
    DC fit + per-match prediction runs ONCE per tournament -- the sweep then varies
    only the cheap pick/settle loop. `conn` is caller-managed (no own-conn here).
    """
    if key not in TOURNAMENTS:
        raise KeyError(f"unknown tournament {key!r}; choose from {list(TOURNAMENTS)}")
    label, tag, first_day, last_day = TOURNAMENTS[key]

    df = train._feature_frame(conn)
    df["date"] = df["date"].astype(str)

    train_df = df[df["date"] < first_day].copy()

    # The feature frame has no tournament tag; select the finals slice by the
    # registered tournament tag + date window from `matches`.
    tour_ids = [r["match_id"] for r in conn.execute(
        "SELECT match_id FROM matches WHERE tournament=? AND date BETWEEN ? AND ?",
        (tag, first_day, last_day))]
    tour_df = df[df["match_id"].isin(tour_ids)].copy().sort_values("date").reset_index(drop=True)

    log.info("%s: train on %d matches < %s; evaluate %d tournament matches",
             label, len(train_df), first_day, len(tour_df))

    params = _fit_slice(train_df)
    cal = _fit_calibrator(train_df, method=calib_method) if calibrate else None

    Xh, Xa = feat_module.design_matrices(tour_df)
    home = tour_df["home_id"].to_numpy(); away = tour_df["away_id"].to_numpy()
    neu = tour_df["neutral"].to_numpy()
    hg = tour_df["home_goals"].to_numpy(); ag = tour_df["away_goals"].to_numpy()

    raw_probs, outs, kept_ids, skipped = [], [], [], 0
    for i in range(len(tour_df)):
        try:
            p = predict_match(params, int(home[i]), int(away[i]),
                              x_home=Xh[i], x_away=Xa[i], neutral=bool(neu[i]))
        except KeyError:
            skipped += 1  # a team had < MIN_TEAM_MATCHES before the cut -> not fit
            continue
        raw_probs.append(p)
        outs.append(_outcome(int(hg[i]), int(ag[i])))
        kept_ids.append(int(tour_df["match_id"].iloc[i]))
    if skipped:
        log.warning("%s: skipped %d/%d matches (team absent from the pre-cut fit)",
                    label, skipped, len(tour_df))

    raw_probs = np.array(raw_probs); outs = np.array(outs)
    cal_probs = cal.transform(raw_probs) if cal is not None else raw_probs.copy()
    return TournamentPredictions(label, raw_probs, cal_probs, outs, kept_ids,
                                 skipped, len(tour_df))


def run_backtest(key: str, *, calibrate: bool = True,
                 calib_method: str = "temperature_draw",
                 min_prob_edge: float = sim.MIN_PROB_EDGE, conn=None) -> BacktestResult:
    """Backtest one tournament. `key` must be in TOURNAMENTS.

    `calib_method` selects the Stage-B calibrator ('temperature' | 'isotonic').
    `min_prob_edge` is the value-picker guard used for the Layer-2 sim (see
    eval/simulator.value_pick); default is the production config value.
    """
    own = conn is None
    conn = conn or connect().__enter__()
    try:
        pred = predict_tournament(conn, key, calibrate=calibrate,
                                  calib_method=calib_method)
        cal_probs, outs, kept_ids = pred.cal_probs, pred.outs, pred.kept_ids

        ece, table = _pooled_reliability(cal_probs, outs)
        result = BacktestResult(
            tournament=pred.label, n_matches=len(outs), n_skipped=pred.n_skipped,
            log_loss_raw=log_loss(pred.raw_probs, outs),
            log_loss_cal=log_loss(cal_probs, outs),
            brier_raw=brier_score(pred.raw_probs, outs),
            brier_cal=brier_score(cal_probs, outs),
            accuracy=float(np.mean(cal_probs.argmax(axis=1) == outs)),
            ece_cal=ece, reliability=table,
        )

        # ---- LAYER 2: ROI, only if historical odds exist for this slice ----
        odds = _load_odds(conn, kept_ids)
        if odds:
            result.roi_by_player = _simulate(cal_probs, outs, kept_ids, odds,
                                             min_prob_edge=min_prob_edge)
            result.odds_note = (f"ROI computed on {len(odds)}/{len(kept_ids)} matches "
                                f"with stored odds (min_prob_edge={min_prob_edge:g}).")
        else:
            result.odds_note = (
                "!! ROI LAYER SKIPPED -- no historical odds in odds_snapshots for "
                f"{pred.label}. The 6-player sim and the ROI gate cannot run. "
                "Resolve open decision O2 (load contemporaneous 1X2 closing odds "
                "via Kaggle/football-data.co.uk free, or The Odds API Historical).")
            log.warning(result.odds_note)
        return result
    finally:
        if own:
            conn.commit(); conn.close()


def _simulate(probs: np.ndarray, outs: np.ndarray, match_ids: list[int],
              odds: dict[int, sim.MatchOdds],
              min_prob_edge: float = sim.MIN_PROB_EDGE) -> dict[str, float]:
    """Run the 6-player flat-stake sim over matches that have odds; return ROI/player.

    model_full and model_fundamental are identical in V1 (no market feature is
    wired yet), so both value-bet off the same calibrated probabilities.
    `min_prob_edge` is the value-picker guard (eval/simulator.value_pick).
    """
    import random
    rng = random.Random(20260611)  # fixed seed: monkey is reproducible
    players = ["model_full", "model_fundamental", "safe", "tide", "risk", "monkey"]
    stakes = {p: 0.0 for p in players}; payouts = {p: 0.0 for p in players}
    for i, mid in enumerate(match_ids):
        if mid not in odds:
            continue
        o = odds[mid]
        mp = sim.ModelProbs(*probs[i].tolist())
        picks = sim.pick_per_player(o, mp, mp, rng, min_prob_edge)
        actual = ("H", "D", "A")[int(outs[i])]
        for pl, pick in picks.items():
            stakes[pl] += sim.STAKE_NIS
            payouts[pl] += sim.settle(pick, actual, o.by_outcome(pick))
    return {pl: roi(np.array([stakes[pl]]), np.array([payouts[pl]])) for pl in players}


def run_all(*, calibrate: bool = True,
            calib_method: str = "temperature_draw") -> list[BacktestResult]:
    with connect() as conn:
        return [run_backtest(k, calibrate=calibrate, calib_method=calib_method,
                             conn=conn) for k in TOURNAMENTS]


def format_report(results: list[BacktestResult]) -> str:
    """Human-readable summary table for the CLI / handoff."""
    lines = []
    lines.append("=" * 78)
    lines.append(f"{'BACKTEST':<14}{'N':>4}{'skip':>5}  "
                 f"{'logloss(raw/cal)':>20}  {'brier(cal)':>11}  {'acc':>6}  {'ECE':>6}  gate")
    lines.append(f"{'baseline':<14}{'':>4}{'':>5}  {UNIFORM_LOG_LOSS:>20.4f}  "
                 f"{UNIFORM_BRIER:>11.4f}  {'.333':>6}  {'':>6}  (uniform 1/X/2)")
    lines.append("-" * 78)
    for r in results:
        gate = "PASS" if r.beats_uniform else "FAIL"
        lines.append(
            f"{r.tournament:<14}{r.n_matches:>4}{r.n_skipped:>5}  "
            f"{r.log_loss_raw:>9.4f}/{r.log_loss_cal:<9.4f}  {r.brier_cal:>11.4f}  "
            f"{r.accuracy:>6.3f}  {r.ece_cal:>6.3f}  {gate}")
    lines.append("=" * 78)
    for r in results:
        if r.odds_note:
            lines.append(f"[{r.tournament}] {r.odds_note}")
        if r.roi_by_player:
            roi_s = "  ".join(f"{p}={v:+.1%}" for p, v in r.roi_by_player.items())
            lines.append(f"[{r.tournament}] ROI: {roi_s}")
    return "\n".join(lines)
