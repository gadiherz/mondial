# MondialPredictionModel

A predictive model for **2026 FIFA World Cup** match outcomes (home / draw / away),
paired with a six-player virtual betting simulation scored against the Israeli
**TOTO WINNER** line.

**Live site:** https://mondial.gadi-herz.workers.dev
**Author:** Gadi Herzlinger

> ⚠️ For fun and research only. Nothing here is betting advice. No real money is
> wagered — the "players" are simulated strategies.

---

## What it does

Every day, on its own, the project:

1. **Pulls data** — historical results, live 1X2 odds, completed scores, and
   qualitative team intel (news / injuries) from free public APIs.
2. **Rates the teams** — a **Glicko-2** rating with a short-term **momentum**
   adjustment (recent form).
3. **Predicts each match** — a **Dixon-Coles** bivariate-Poisson goal model turns
   the ratings into probabilities for home win / draw / away win.
4. **Calibrates** — **temperature scaling** (Guo et al. 2017) keeps the
   probabilities honest, cross-validated on past tournaments.
5. **Runs the betting sim** — six strategies (two model variants, plus "safe",
   "draw-tide", "risk", and a random "monkey") each stake a flat ₪10 per match on
   the match day, settled at the Winner price once the result is in.
6. **Publishes** the predictions, odds, and player leaderboard to the live site.

The model methods are documented inline; key academic sources: Elo (1978),
Glickman (Glicko-2, 2012), Dixon & Coles (1997), Guo et al. (temperature
scaling, 2017).

## How it stays live without anyone touching it

You don't run anything by hand once it's deployed:

- **GitHub Actions** runs the Python on a schedule (a daily "markets-intel" job and
  a 2-hourly "results" job — see `.github/workflows/`).
- Each run writes fresh numbers into **`web/data/dashboard.json`** and commits it.
- **Cloudflare** watches the repo and re-publishes the static site within minutes.

So: *GitHub clock → runs Python → writes a data file → Cloudflare re-publishes.*
The website (`web/`) is plain HTML/CSS/JavaScript with **zero external
dependencies** — it just reads that one data file. See `DEPLOY.md` for the full
deployment + security setup.

## Repository layout

```
src/mondial/        the Python package (the "kitchen")
  data/             SQLite store + schema (data/mondial.db)
  model/            Glicko-2, Dixon-Coles, calibration, knockout bracket
  eval/             betting simulation, leaderboard, backtests, odds handling
  scrapers/         Winner-odds reader (bankerim aggregator)
  pipelines/        the runnable jobs (train, markets_intel, results, daily_run,
                    export_web)
scripts/            one-off / diagnostic helpers (backtest, sweeps, inspectors)
web/                the static website served to the public
  index.html        home: today's matches, odds, model pick, countdowns
  dashboard.html    six-player leaderboard + revenue chart
  engine.html       plain-language explainer of the model
  data/             dashboard.json — the handoff file the site reads
.github/workflows/  the scheduled automation (Actions) + secret scan (gitleaks)
tests/              pytest suite
DEPLOY.md           step-by-step deploy + security checklist
```

## Quickstart (run it locally)

Requires **Python 3.11+**.

```bash
# 1. Install the package (uv works too: `uv pip install -e ".[dev]"`)
python -m pip install -e ".[dev]"

# 2. Set up secrets — copy the template and fill in your own free API keys
cp .env.example .env          # then edit .env

# 3. Create the local SQLite database
python -m mondial.data.db init

# 4. Pull historical data, fit the model, calibrate it
python -m mondial.pipelines.train --bootstrap --fit --calibrate

# 5. Run one full daily cycle (predict + place/settle bets + publish web data)
python -m mondial.pipelines.daily_run

# 6. Preview the website locally, then open http://localhost:8000
cd web && python -m http.server 8000
```

The two automated jobs can also be run manually:

```bash
python -m mondial.pipelines.markets_intel   # odds + intel + predictions (Routine A)
python -m mondial.pipelines.results         # ingest scores + settle bets (Routine B)
```

API keys (all free tiers; see `.env.example` for sign-up links). None are required
just to explore the code or run the tests:

| Key | Used for |
|-----|----------|
| `ODDS_API_KEY` | live 1X2 odds + completed scores |
| `INTEL_API_KEY` | team-intel summariser (Groq free tier by default) |
| `GUARDIAN_API_KEY`, `WORLDNEWS_API_KEY`, `APIFOOTBALL_API_KEY` | news / injury intel (each optional, degrades gracefully) |

## Tests

```bash
pytest          # full suite
```

## Status

**V1 — deployed and live.** Self-running via GitHub Actions + Cloudflare.
World Cup kickoff: **2026-06-11** (the leaderboard sits at ₪0 until the first
match day, then accrues as match-days arrive).

## License

No open-source license is granted. The code is shared for viewing and personal,
non-commercial experimentation. Feel free to fork and tinker; please don't
redistribute or use commercially without permission.
