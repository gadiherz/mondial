# Mondial 2026 — Handoff / Resume Note

_Last updated: 2026-06-05._

WC 2026 kickoff: **2026-06-11**. Repo still lives in Google Drive and is **not
git-init'd** yet. Everything runs locally with `PYTHONPATH=src` (not pip-installed).

---

## TL;DR — where we are

The **backend core engine is complete and self-running.** Data → Glicko-2 +
momentum (leakage-free) → Dixon-Coles fit → isotonic calibration → live odds →
API-sourced semantic team-intel (`form_outlook`) → value picks, all wrapped in
two scheduled GitHub Actions routines, with a secret-scan guard. **15/15 tests
pass.** What's left is the **TOTO scraper**, the **frontend**, and **deployment**.

### Updates since 2026-05-31 (read these — they supersede older notes below)

- **§7.3 backtest BUILT + RUN** (`eval/backtest.py`, `scripts/run_backtest.py`).
  Raw model beats the uniform 1/X/2 floor on WC18/WC22/Euro24/Copa24 (real skill).
- **CALIBRATION FIXED (2026-06-05).** The old per-class isotonic calibrator
  over-shifted upset-heavy **WC22** (cal log-loss 1.195 > 1.099 floor → FAIL) and
  in fact hurt on ALL held-out tournaments (it overfits its holdout). Replaced
  with single-parameter **temperature scaling** (`model/calibration.py::
  TemperatureScaler`, Guo et al. 2017): can't overfit or collapse a class. **All
  four tournaments now PASS** (WC18 .980 / WC22 1.058 / Euro24 .968 / Copa24 .886);
  production fit T≈0.86 (mild sharpening), holdout log-loss 0.847→0.843. Calibrator
  is method-tagged JSON; `calibration.load_calibrator` dispatches isotonic vs
  temperature. 22 tests pass (added `tests/test_calibration.py`). The live-betting
  calibration gate is cleared.
- **O2 free odds path (Layer-2 ROI)**: `scrapers/historical_odds.py` +
  `scripts/ingest_historical_odds.py` load WC2022 odds free from the football-
  data.co.uk World Cup workbook (64/64 matched). WC22 model ROI **+59.3%** but
  dumb `risk` +68.2% beats it → upset-variance, not proven edge. **Euro24/Copa24
  are NOT on football-data → ingest a CSV via the generic `--csv` path.**
- **Contrarian min-edge guard DONE** (`eval/simulator.value_pick`,
  `config.MIN_PROB_EDGE=0.05`, `scripts/sweep_min_edge.py`). Guards the value
  picker from chasing long-odds noise: deserts the favorite only when the model's
  prob beats the book's implied prob by ≥ `min_prob_edge` (probability space).
  Swept on WC22 (only tournament with odds so far); cross-validation across
  tournaments is pending the Euro24/Copa24 odds above.
- **Doc-drift pass (2026-06-05)**: ARCHITECTURE §5/§7.1, `intel/__init__.py`,
  `scripts/fetch_intel.py`, `daily_run.py`, `.env.example` corrected — intel uses
  free APIs + **Groq** (not the Claude API), and the old single `daily.yml` is
  gone (two routines now).
- **LIVE 6-player sim DONE (2026-06-05)** — `eval/betting.py` (place_bets /
  settle_bets / leaderboard) persists bets to the `bets` table at the Winner line,
  wired into Routine A (place) + Routine B (settle), `scripts/leaderboard.py`,
  `UNIQUE(player_name,match_id)` index for idempotent placement. Validated: 60 bets
  placed for 10 priced WC-2026 matches.
- **KNOCKOUT BRACKET DONE (2026-06-05)** — `model/bracket.py`: group standings (FIFA
  tiebreakers incl. head-to-head) → top-2 + 8-best-thirds → official R32 template +
  R16→final tree → `generate_knockout` writes knockout fixtures into `matches`
  (stage/bracket_no cols added; `UNIQUE(bracket_no)`); wired into Routine B,
  `scripts/bracket.py`. GROUPS verified == the real DB draw (Mexico=A…); date-scoped
  to 2026 so historical WCs don't leak. Thirds via bipartite matching + Annex-C
  override hook. 8 tests. **This was the last BACKEND task — the backend is now
  COMPLETE.** Remaining work is all frontend §2-4 / deploy / key-rotation / V2.

---

## DONE — built & verified

- **Data** (`data/mondial.db`): 11,700 final matches + 72 WC group-stage fixtures;
  `match_features` (per-match leakage-free Glicko+momentum), `team_state`,
  `odds_snapshots` (live Pinnacle etc.), `predictions`, `team_intel`.
- **Rating engine**: Glicko-2 + momentum (`model/glicko.py`, `model/ratings.py`).
  Replaced point-Elo (kept only as a legacy column).
- **Model**: Dixon-Coles fit on 11,259 matches (`data/dc_params.json`) with 5
  features: `glicko_diff, rd_sum, resid_ewma_diff, gd_ewma_diff, idle_days_diff`.
  Stage-B isotonic calibration (`data/calibrator.json`).
- **Odds**: The Odds API live; **value picker** (`pick = argmax p·odd`, the
  decided rule, NOT argmax-probability) in `eval/simulator.py`.
- **Team-intel (Track B) — the "black-swan" edge engine**:
  APIs → grounding → semantic distillation → LLM → `form_outlook` vector →
  Glicko overlay. Specifically:
  - Sources (all free APIs, datacenter-friendly): **Guardian Content API**
    (full text, primary), **World News API** (full text, redundancy),
    **API-Football** (structured injuries). `intel/providers.py`.
  - **Roster grounding** from Wikipedia squad (`intel/roster.py`) — keeps only
    sentences about THIS team's players.
  - **Semantic contrastive distillation** (`intel/retrieve.py`): local fastembed
    ONNX; rank by similarity to performance anchors MINUS transfer/club
    anti-anchors. No keyword lists.
  - **LLM scoring** (`intel/processor.py`): Groq `llama-3.3-70b` (free), JSON
    mode, scores ONLY from the passages; `form_outlook` is the dominant
    (0.45-weight) black-swan dimension. `composite = weighted-mean × confidence`.
  - **Injection**: `features.build_feature_vector` nudges effective Glicko by
    `composite × intel_glicko_points` (150) → rides the fitted glicko_diff weight.
  - Data-flow safety: bounded input/call, per-day stop-and-defer vs per-minute
    pace, never fabricates (failure → skip, logged). Per-prediction provenance
    (`qualitative_used`, `intel_*_status` ∈ informed/no_signal/missing).
- **Schedulers** (the two routines, by data nature):
  - `pipelines/markets_intel.py` + `.github/workflows/markets-intel.yml`
    (Routine A: odds + play-day intel + predict + publish; cron 14:00 UTC daily).
  - `pipelines/results.py` + `.github/workflows/results.yml`
    (Routine B: **Jurisoo CSV refresh (primary, key-less, catches prep
    friendlies)** + Odds API `/scores` (supplementary, failure-isolated) →
    finalize matches → re-backfill Glicko/momentum; cron 2h). `refresh_
    international_results()` promotes scheduled→final (the base upsert is
    INSERT-OR-IGNORE and couldn't). Ran 2026-06-03: ingested 39 played matches
    through 2026-06-02 (June prep window), DB now 11,739 finals; momentum moved
    as expected (e.g. Serbia's 0-3 shock vs Cape Verde drove resid_ewma to
    -0.080). Next predict round (Routine A) reflects the updated team_state.
  - Shared `concurrency: mondial-data` lock to avoid commit races.
- **Secret guard**: `.gitleaks.toml` + `.github/workflows/gitleaks.yml` +
  `.pre-commit-config.yaml`. Keys in `.env` (gitignored); verified 0 key values
  anywhere else in the repo.

## Keys (in `.env`, gitignored) — **ROTATE before deploy / going public**

`ODDS_API_KEY`, `INTEL_API_KEY` (Groq), `GUARDIAN_API_KEY`, `WORLDNEWS_API_KEY`,
`APIFOOTBALL_API_KEY`. They passed through a chat transcript → regenerate them
and put the new values in **GitHub Actions Secrets** at deploy time.

---

## TODO — what's left (in the user's priority order)

1. **TOTO/Winner scraper — DONE (2026-06-05).** NOT a headless browser. The
   official `winner.co.il/api/v2/publicapi/` API was found (clean JSON) but is
   Incapsula-bot-walled (won't run from CI). Solved via the **bankerim.co.il
   aggregator**, which mirrors Winner's line server-rendered with no bot wall
   (CI-safe). `scrapers/winner_odds.py` parses football full-match 1X2 cards,
   maps Hebrew nation names → DB English (`HE_TEAM_ALIASES`), writes
   `bookmaker='winner'` to odds_snapshots; wired into Routine A
   (`scrape_winner`), `scripts/fetch_winner_odds.py`, 3 tests. Validated live:
   10 national-team matches priced. Extend HE_TEAM_ALIASES as new spellings log.
   **EVAL REWIRED vs Winner (2026-06-05):** `eval/odds.load_eval_odds` (strict
   `bookmaker='winner'`, no fallback) is now the value-pick/settlement price;
   sharp Pinnacle stays the "true market" edge reference only. `predict_upcoming`
   output carries `winner_*` + `value_vs='winner'`; `value_picks.py` shows
   Winner. Historical backtest stays on `fd_avg` (no archived Winner odds).
   Validated: 72 predictions, 10 WC-2026 matches priced vs Winner. 28 tests pass.
2. **Frontend** — DONE: a self-contained static site in `web/` (hand-written
   HTML/CSS/vanilla-JS, zero external deps) reading `web/data/dashboard.json`
   from `pipelines/export_web.py`. Replaces the earlier Streamlit plan. Three
   pages: home (matches/odds/model pick + countdowns), players dashboard
   (leaderboard + revenue chart), engine explainer. See README + DEPLOY.md.
3. **Deploy**: DONE — repo on GitHub (private→public), served by Cloudflare
   (output dir `web`), 5 keys in Actions Secrets, both crons live. Full
   step-by-step in `DEPLOY.md`.
4. **Rotate keys** — DONE (4/5; Guardian deferred as accepted low risk).
5. **Evaluation loop**: the §7.3 pre-kickoff **backtest is BUILT and RUN**
   (`eval/backtest.py` + `scripts/run_backtest.py`, 2026-06-03). Layer-1
   skill/calibration runs on real WC22/Euro24/Copa24 results (no odds). Findings:
   the **raw** model beats the uniform floor on all three (log-loss
   1.048/0.967/0.887, ~50–59% acc, ECE ≤0.046 — real skill); **calibrated**
   passes Euro24+Copa24 but **FAILS WC22** (1.195 > 1.099) because the isotonic
   calibrator over-confidently shifts the upset-heavy WC. Open item: milder /
   tournament-aware calibration before live betting. **Layer-2 ROI is BLOCKED** —
   no historical odds (`odds_snapshots` is WC-2026-only; open decision O2). Still
   TODO: the live 6-player sim that persists to `bets` (frontend leaderboard dep).
   **2026-06-04: Layer-2 ROI WIRED via the free path** (O2). `scrapers/historical_
   odds.py` + `scripts/ingest_historical_odds.py` load WC-2022 odds for free from
   the football-data.co.uk World Cup workbook (per-tournament sheets, no key);
   64/64 matches matched, 0 unresolved. Backtest scores vs the consensus line
   (fd_avg; best-price excluded). **WC22 model ROI +59.3%** (safe −12.6/tide
   −18.3/monkey −22.3) BUT dumb `risk`/longshot +68.2% beats it — upset-tournament
   variance, not proven edge. Euro24/Copa24 NOT on football-data → drop a Kaggle
   CSV and ingest via the generic `--csv` path. Next: Euro24 odds for corroboration
   + min-edge floor on the value picker + calibration fix.
6. **Knockout/playoff bracket (not built)** — DB has only the group stage; R32→
   final + 8-best-thirds standings logic is unbuilt.

## Open tuning knobs (noted, not yet tuned)

- Intel: `composite` weights (form 0.45 / avail 0.25 / others 0.10),
  `intel_glicko_points` (150), `intel_play_window_days` (1), distillation
  `min_score`/`_ANTI_WEIGHT`. Value picker: no **min-edge / prob-floor** guard yet
  (it can chase long-odds draws). DC fit emits a benign non-convergence warning
  (225-team dimensionality). §6.3 time-series CV not done (single-holdout only).
- Intel is **PREDICTION-ONLY, not backtestable** — validate forward vs live picks.
- World News API free tier is **50 pts/day** → the play-day scoping is essential.
- `markets-intel.yml` cron (14:00 UTC) should be tuned to ~2h before real kickoffs.
- Intel `team_intel` table currently holds stale headline-era rows + a few deep
  test rows; it repopulates itself per play-day once deployed (no value now,
  2 weeks out — by design).

## How to run (local, `PYTHONPATH=src`)

```
# train / refit / recalibrate
PYTHONPATH=src python -m mondial.pipelines.train --bootstrap --fit --calibrate
# Routine A (odds + intel + predict + publish)
PYTHONPATH=src python -m mondial.pipelines.markets_intel
# Routine B (ingest results + re-backfill)
PYTHONPATH=src python -m mondial.pipelines.results
# manual intel / odds fetch, value-pick view
PYTHONPATH=src python scripts/fetch_intel.py [--limit N]
PYTHONPATH=src python scripts/fetch_odds.py
PYTHONPATH=src python scripts/value_picks.py
# tests
PYTHONPATH=src python -m pytest -q
```

See `ARCHITECTURE.md` for the full design.
