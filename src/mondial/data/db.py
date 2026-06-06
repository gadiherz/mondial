"""SQLite store. Schema matches ARCHITECTURE.md §4.2."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

from mondial.config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS teams (
    team_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    confederation TEXT NOT NULL,
    fifa_rank INTEGER,
    fifa_rank_date TEXT
);

CREATE TABLE IF NOT EXISTS players (
    player_id INTEGER PRIMARY KEY,
    team_id INTEGER REFERENCES teams(team_id),
    name TEXT NOT NULL,
    value_eur REAL,
    value_date TEXT
);

CREATE TABLE IF NOT EXISTS coaches (
    coach_id INTEGER PRIMARY KEY,
    team_id INTEGER REFERENCES teams(team_id),
    name TEXT NOT NULL,
    intl_wins INTEGER DEFAULT 0,
    intl_matches INTEGER DEFAULT 0,
    prev_wc_or_euro INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS matches (
    match_id INTEGER PRIMARY KEY,
    date TEXT NOT NULL,
    home_id INTEGER REFERENCES teams(team_id),
    away_id INTEGER REFERENCES teams(team_id),
    tournament TEXT NOT NULL,
    home_goals INTEGER,
    away_goals INTEGER,
    neutral INTEGER NOT NULL DEFAULT 0,         -- 1 if venue is neutral
    status TEXT NOT NULL DEFAULT 'scheduled',   -- scheduled | live | final
    stage TEXT,                                 -- knockout stage (R32/R16/QF/SF/3P/F); NULL=group
    bracket_no INTEGER,                          -- WC fixture number 73..104 for knockout slots
    UNIQUE(date, home_id, away_id)
);

CREATE TABLE IF NOT EXISTS odds_snapshots (
    snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id INTEGER REFERENCES matches(match_id),
    bookmaker TEXT NOT NULL,
    ts TEXT NOT NULL,
    odd_home REAL, odd_draw REAL, odd_away REAL
);

CREATE TABLE IF NOT EXISTS ratings (
    team_id INTEGER REFERENCES teams(team_id),
    date TEXT NOT NULL,
    elo REAL,
    dc_attack REAL,
    dc_defence REAL,
    PRIMARY KEY (team_id, date)
);

CREATE TABLE IF NOT EXISTS predictions (
    match_id INTEGER PRIMARY KEY REFERENCES matches(match_id),
    p_home REAL, p_draw REAL, p_away REAL,
    predicted_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bets (
    bet_id INTEGER PRIMARY KEY AUTOINCREMENT,
    player_name TEXT NOT NULL,
    match_id INTEGER REFERENCES matches(match_id),
    pick TEXT NOT NULL,         -- 'H' | 'D' | 'A'
    stake REAL NOT NULL,
    odd REAL NOT NULL,
    payout REAL,
    settled INTEGER DEFAULT 0
);

-- Per-match PRE-match state, recorded by the leakage-free streaming backfill
-- (mondial.model.ratings). Every column is computed from matches strictly
-- BEFORE this one, so the row is safe to use as a training feature for this
-- match. Stored from the home team's perspective; the away perspective is
-- derived by sign-flipping antisymmetric features (see features.py).
CREATE TABLE IF NOT EXISTS match_features (
    match_id INTEGER PRIMARY KEY REFERENCES matches(match_id),
    date TEXT NOT NULL,
    home_glicko REAL, away_glicko REAL,     -- pre-match Glicko rating (mu)
    home_rd REAL, away_rd REAL,             -- pre-match rating deviation (phi)
    home_resid_ewma REAL, away_resid_ewma REAL,  -- momentum: perf vs expectation
    home_gd_ewma REAL, away_gd_ewma REAL,   -- recent goal-difference form
    home_idle_days REAL, away_idle_days REAL
);

-- Latest per-team Glicko + momentum snapshot, overwritten on each backfill.
-- Used to build features for upcoming (scheduled) matches that have no result
-- yet and therefore no match_features row.
CREATE TABLE IF NOT EXISTS team_state (
    team_id INTEGER PRIMARY KEY REFERENCES teams(team_id),
    as_of_date TEXT NOT NULL,
    glicko REAL, rd REAL, vol REAL,
    resid_ewma REAL, gd_ewma REAL,
    last_match_date TEXT
);

-- LLM-derived qualitative "intel" vector per team (Track B). Written by the
-- autonomous logger (mondial.intel) from current web sources; signed scores in
-- [-1, 1] (positive = favourable), confidence in [0, 1]. `composite` is the
-- single value the prediction path uses to nudge the team's effective Glicko.
-- PREDICTION-ONLY: not present for historical matches, never used in the fit;
-- it shifts the rating at predict time and rides the fitted glicko_diff weight.
CREATE TABLE IF NOT EXISTS team_intel (
    team_id INTEGER PRIMARY KEY REFERENCES teams(team_id),
    as_of_date TEXT NOT NULL,
    form_outlook REAL,
    availability REAL, coach_stability REAL, morale REAL, motivation REAL,
    confidence REAL, composite REAL,
    summary TEXT, model TEXT, fetched_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_matches_date ON matches(date);
CREATE INDEX IF NOT EXISTS idx_odds_match ON odds_snapshots(match_id);
CREATE INDEX IF NOT EXISTS idx_match_features_date ON match_features(date);
-- One bet per (player, match): lets the live sim place idempotently
-- (INSERT OR IGNORE) so re-runs never duplicate or re-price a placed bet.
CREATE UNIQUE INDEX IF NOT EXISTS idx_bets_player_match ON bets(player_name, match_id);
-- NB: the partial unique index on matches(bracket_no) is created in init_db AFTER
-- the bracket_no column migration (it can't live here -- on an existing DB the
-- column is added by ALTER below, after this script runs).
"""


@contextmanager
def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(path: Path = DB_PATH) -> None:
    with connect(path) as conn:
        conn.executescript(SCHEMA)
        # Lightweight migrations for columns added after a DB was first created.
        cols = {r[1] for r in conn.execute("PRAGMA table_info(team_intel)")}
        if "form_outlook" not in cols:
            conn.execute("ALTER TABLE team_intel ADD COLUMN form_outlook REAL")
        mcols = {r[1] for r in conn.execute("PRAGMA table_info(matches)")}
        if "stage" not in mcols:
            conn.execute("ALTER TABLE matches ADD COLUMN stage TEXT")
        if "bracket_no" not in mcols:
            conn.execute("ALTER TABLE matches ADD COLUMN bracket_no INTEGER")
        # One match per knockout bracket slot (idempotent generation). Created here,
        # after the column exists on both fresh and migrated DBs.
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_matches_bracket "
                     "ON matches(bracket_no) WHERE bracket_no IS NOT NULL")


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "init":
        init_db()
        print(f"Initialised {DB_PATH}")
    else:
        print("Usage: python -m mondial.data.db init")
