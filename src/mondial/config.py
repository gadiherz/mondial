from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "mondial.db"
DC_PARAMS_PATH = DATA_DIR / "dc_params.json"
CALIBRATOR_PATH = DATA_DIR / "calibrator.json"

# Value-picker min-edge guard (ARCHITECTURE.md §7.1). The model player deserts the
# market favorite for a draw/underdog only when its probability exceeds the book's
# margin-free implied probability by at least this many probability points;
# otherwise it falls back to the favorite. 0.0 = pure-EV (no guard).
#
# CALIBRATED 0.08 by cross-validation (scripts/sweep_min_edge.py, 2026-06-05) on the
# two tournaments with free consensus odds, WC 2018 + WC 2022. 0.08 is the lowest
# threshold at which BOTH tournaments' contrarian picks beat their breakeven hit-rate
# (edge=yes) with positive ROI; at the old theory-floor 0.05, WC 2018's contrarian
# bets are still below breakeven (noise). The plateau holds to ~0.12 but thins the
# bet count. Re-run the sweep (ideally adding Euro24/Copa24 odds) before changing it.
MIN_PROB_EDGE = 0.08


class Settings(BaseSettings):
    odds_api_key: str = ""
    football_data_token: str = ""
    api_football_key: str = ""
    tz: str = "Asia/Jerusalem"

    # --- LLM team-intel feature (Track B): free source -> hosted LLM -> vector ---
    # Stage 2 is any OpenAI-compatible chat endpoint, so it swaps by config only:
    #   Groq (default, free):  https://api.groq.com/openai/v1     llama-3.3-70b-versatile
    #   Gemini (free):         https://generativelanguage.googleapis.com/v1beta/openai
    #                          gemini-2.0-flash
    #   local Ollama:          http://localhost:11434/v1          llama3.1
    intel_base_url: str = "https://api.groq.com/openai/v1"
    intel_model: str = "llama-3.3-70b-versatile"
    intel_api_key: str = ""   # INTEL_API_KEY (provider key; empty for local Ollama)
    # Corpus source APIs (datacenter-friendly, deploy-and-forget). All optional;
    # the multi-provider fetch uses whatever keys are present.
    guardian_api_key: str = ""      # full-text journalism (open-platform.theguardian.com)
    worldnews_api_key: str = ""     # full-text redundancy (worldnewsapi.com)
    apifootball_api_key: str = ""   # structured injuries/lineups (dashboard.api-football.com)
    # "Full power": a maxed-out (composite=+/-1) intel reading shifts a team's
    # effective Glicko rating by this many points before glicko_diff is computed,
    # so it flows through the already-fitted glicko_diff weight.
    intel_glicko_points: float = 150.0
    # Routine A refreshes intel only for teams playing within this many days
    # ("once per play-day"): 0 = today only, 1 = today + tomorrow buffer.
    intel_play_window_days: int = 1
    # --- Data-flow safety (don't blow the provider's per-min / per-day budget) ---
    # Bound the text sent per team so each call's token cost is predictable.
    intel_max_context_chars: int = 4000
    # Per-DAY guard: stop the run and DEFER remaining teams (not zero them) when
    # the provider's remaining-REQUESTS header (daily bucket) drops below this.
    intel_min_remaining_requests: int = 2
    # Per-MINUTE guard: when remaining-TOKENS (per-minute bucket, refills) drops
    # below this, PACE (brief wait) rather than stop -- data keeps flowing.
    intel_min_remaining_tokens: int = 1500

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


settings = Settings()
