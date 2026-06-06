# MondialPredictionModel

Predictive model for the 2026 FIFA World Cup match outcomes (1/X/2), with a virtual betting simulation against Israeli TOTO WINNER 16.

See `../ARCHITECTURE.md` for the full design.

## Quickstart (dev)

```bash
# 1. Move this skeleton out of Google Drive into a normal working directory.
#    Drive sync + git do not play well together.
cp -r repo_skeleton ~/dev/mondial && cd ~/dev/mondial && git init

# 2. Install (uv recommended; pip works too)
uv venv && source .venv/bin/activate
uv pip install -e ".[dev]"

# 3. Set up secrets
cp .env.example .env
# Edit .env with API keys for: ODDS_API_KEY, FOOTBALL_DATA_TOKEN, API_FOOTBALL_KEY

# 4. Initialise the SQLite store
python -m mondial.data.db init

# 5. Pull initial historical data
python -m mondial.pipelines.train --bootstrap

# 6. Fit the model
python -m mondial.pipelines.train --fit

# 7. Run a one-off daily prediction cycle
python -m mondial.pipelines.daily_run

# 8. Launch the dashboard locally
streamlit run app/streamlit_app.py
```

## Repository layout

See `../ARCHITECTURE.md` §10.

## Status

V1 in development. WC 2026 kickoff: 2026-06-11.
