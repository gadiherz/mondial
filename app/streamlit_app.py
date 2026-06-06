"""Streamlit dashboard. See ARCHITECTURE.md §9."""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import streamlit as st

from mondial.config import SNAPSHOT_PATH

st.set_page_config(page_title="Mondial 2026", layout="wide")
st.title("Mondial 2026 — daily predictions")

if not SNAPSHOT_PATH.exists():
    st.warning("No snapshot yet. Run `python -m mondial.pipelines.daily_run` first.")
    st.stop()

snapshot = json.loads(Path(SNAPSHOT_PATH).read_text())
st.caption(f"Generated at {snapshot['generated_at']}")

# --- Section 1: today's predictions ---
st.header("Today's predictions")
preds = pd.DataFrame(snapshot["predictions"])
if preds.empty:
    st.info("No matches scheduled in the next 36 hours.")
else:
    if "qualitative_used" in preds.columns:
        # Explicit provenance badge so a reader knows whether a prediction was
        # informed by qualitative (LLM-intel) info or is ratings-only.
        preds.insert(0, "qual.", preds["qualitative_used"].map(
            {True: "🟢 intel", False: "⚪ ratings-only"}))
        n_used = int(preds["qualitative_used"].sum())
        st.caption(f"Qualitative team-intel applied to {n_used}/{len(preds)} "
                   f"predictions (🟢). ⚪ = ratings only (no intel / no signal / deferred).")
    st.dataframe(preds, use_container_width=True)

# --- Section 2: leaderboard ---
st.header("Leaderboard")
st.write("TODO: query bets table, compute per-player bankroll + ROI.")

# --- Section 3: bankroll curves ---
st.header("Bankroll curves")
st.write("TODO: line chart of cumulative P&L per player over match index.")

# --- Section 4: calibration ---
st.header("Calibration & metrics")
st.write("TODO: reliability diagram, Brier, log-loss running totals.")
