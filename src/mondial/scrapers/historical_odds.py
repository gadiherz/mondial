"""Historical 1X2 odds loader -- back-fills `odds_snapshots` for past tournaments
so the backtest's ROI / "beat the market" layer can run (open decision O2, the
free path).

Two sources, one ingest core:

  * **football-data.co.uk World Cup workbook** (`WorldCup2026.xlsx`) -- FREE, no
    key, no auth. Despite the file name it carries one sheet PER tournament:
    `WorldCup2022`, `WorldCup2018`, `WorldCup2014` (+ the 2026 qualifiers), each
    with per-match average (`H_Avg/D_Avg/A_Avg`) and best (`H_Max/D_Max/A_Max`)
    decimal odds. This covers the backtest's primary tournament (WC 2022) with
    zero manual work.
  * **generic CSV** (`ingest_csv`) -- for any per-match 1X2 file you drop in
    `data/historical_odds/` (e.g. a Kaggle Euro-2024 export). You give it the
    date / team / odds column names; the resolution + idempotent write are shared.

Team and match resolution REUSE the live Odds-API path (`_norm`, `NAME_ALIASES`,
`resolve_team_id`, `resolve_match`) so historical and live odds land on the same
DB match rows by team identity (handles neutral-venue home/away swaps). Writes
are idempotent: each (match_id, bookmaker) is cleared before re-insert, so a
re-run never duplicates. Anything unresolved is logged loudly, never dropped
silently.
"""
from __future__ import annotations

import io
import logging
from pathlib import Path

import pandas as pd
import requests

from mondial.scrapers.odds_api import _norm, _norm_index, resolve_match, resolve_team_id

log = logging.getLogger("historical_odds")

FD_WORKBOOK_URL = "https://www.football-data.co.uk/WorldCup2026.xlsx"
# football-data sheet name -> (our tournament tag, finals date window) for the
# tournaments the backtest knows about. Sheets carry friendlies/quali rows too;
# the date window + match resolution keep only the real finals matches.
FD_SHEETS: dict[str, str] = {
    "WorldCup2022": "WC 2022",
    "WorldCup2018": "WC 2018",
    "WorldCup2014": "WC 2014",
}


def fetch_fd_workbook() -> pd.ExcelFile:
    """Download the football-data.co.uk World Cup workbook (all sheets)."""
    resp = requests.get(FD_WORKBOOK_URL, timeout=60)
    resp.raise_for_status()
    return pd.ExcelFile(io.BytesIO(resp.content))


def _ingest_rows(
    conn,
    df: pd.DataFrame,
    *,
    date_col: str,
    home_col: str,
    away_col: str,
    odds_cols: dict[str, tuple[str, str, str]],   # bookmaker_tag -> (H, D, A) cols
    source: str,
    window_days: int = 2,
) -> dict[str, int]:
    """Resolve each row to a DB match and write its odds. Idempotent per
    (match_id, bookmaker). Returns a counters dict."""
    idx = _norm_index(conn)
    c = {"matched": 0, "written": 0, "unresolved_team": 0, "no_match": 0, "skipped_na": 0}
    unresolved_names: set[str] = set()

    for _, row in df.iterrows():
        h_name, a_name = str(row[home_col]), str(row[away_col])
        h_id = resolve_team_id(h_name, idx)
        a_id = resolve_team_id(a_name, idx)
        if h_id is None or a_id is None:
            if h_id is None:
                unresolved_names.add(h_name)
            if a_id is None:
                unresolved_names.add(a_name)
            c["unresolved_team"] += 1
            continue

        d = pd.to_datetime(row[date_col], dayfirst=False, errors="coerce")
        if pd.isna(d):
            c["skipped_na"] += 1
            continue
        # resolve_match takes a tz-aware datetime; date precision is enough here.
        from datetime import UTC, datetime
        commence = datetime(d.year, d.month, d.day, tzinfo=UTC)
        m = resolve_match(conn, h_id, a_id, commence)
        if m is None:
            c["no_match"] += 1
            continue

        db_home_is_h = m["home_id"] == h_id  # else the source has home/away swapped
        ts = d.date().isoformat()             # deterministic: odds dated to the match
        c["matched"] += 1
        for tag, (hc, dc, ac) in odds_cols.items():
            oh, od, oa = row.get(hc), row.get(dc), row.get(ac)
            if any(pd.isna(v) for v in (oh, od, oa)):
                c["skipped_na"] += 1
                continue
            odd_home = float(oh) if db_home_is_h else float(oa)
            odd_away = float(oa) if db_home_is_h else float(oh)
            bookmaker = f"{source}_{tag}"
            conn.execute(
                "DELETE FROM odds_snapshots WHERE match_id=? AND bookmaker=?",
                (m["match_id"], bookmaker),
            )
            conn.execute(
                """INSERT INTO odds_snapshots
                   (match_id, bookmaker, ts, odd_home, odd_draw, odd_away)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (m["match_id"], bookmaker, ts, odd_home, float(od), odd_away),
            )
            c["written"] += 1

    if unresolved_names:
        log.warning("%s: %d unresolved team name(s) -> add to NAME_ALIASES: %s",
                    source, len(unresolved_names), sorted(unresolved_names))
    log.info("%s: matched %d rows, wrote %d odds rows; skipped %d (unknown team), "
             "%d (no DB match), %d (missing odds cells)",
             source, c["matched"], c["written"], c["unresolved_team"],
             c["no_match"], c["skipped_na"])
    return c


def _fd_odds_cols(columns) -> dict[str, tuple[str, str, str]]:
    """Pick the odds-column triples present in a football-data sheet.

    Per-tournament sheets use HYPHEN names (`H-Avg`, `H-Max`, and a real book
    `bet365-*` for 2022 / `Pinny-*` for 2018). Always take avg (closing-line
    consensus) + max (best price); add the sharp book column if present.
    """
    cols = set(columns)
    out: dict[str, tuple[str, str, str]] = {}
    if {"H-Avg", "D-Avg", "A-Avg"} <= cols:
        out["avg"] = ("H-Avg", "D-Avg", "A-Avg")
    if {"H-Max", "D-Max", "A-Max"} <= cols:
        out["max"] = ("H-Max", "D-Max", "A-Max")
    for tag, pre in (("pinnacle", "Pinny"), ("bet365", "bet365"),
                     ("betfair_exch", "Betfair_Exch")):
        trip = (f"{pre}-H", f"{pre}-D", f"{pre}-A")
        if set(trip) <= cols:
            out[tag] = trip
    if not out:
        raise ValueError(f"no recognised odds columns in sheet; saw {sorted(cols)}")
    return out


def ingest_fd_worldcup(conn, sheet: str = "WorldCup2022") -> dict[str, int]:
    """Ingest one football-data World Cup sheet's per-match odds.

    Writes `fd_avg` (market-average closing line, the consensus the backtest
    scores value against), `fd_max` (best price, feeds the `risk`/`safe` players
    a real best-odds spread), and the sheet's sharp book (`fd_pinnacle` /
    `fd_bet365`) when present.
    """
    xl = fetch_fd_workbook()
    if sheet not in xl.sheet_names:
        raise KeyError(f"{sheet!r} not in workbook sheets {xl.sheet_names}")
    df = xl.parse(sheet)
    return _ingest_rows(
        conn, df, date_col="Date", home_col="Home", away_col="Away",
        odds_cols=_fd_odds_cols(df.columns), source="fd",
    )


def ingest_csv(
    conn, path: str | Path, *,
    date_col: str, home_col: str, away_col: str,
    odds_cols: dict[str, tuple[str, str, str]], source: str | None = None,
) -> dict[str, int]:
    """Ingest a generic per-match 1X2 CSV (e.g. a Kaggle Euro-2024 export).

    `odds_cols` maps a short bookmaker tag to its (home, draw, away) column names,
    e.g. {"b365": ("B365H", "B365D", "B365A")}. `source` defaults to the file stem.
    """
    path = Path(path)
    df = pd.read_csv(path)
    return _ingest_rows(
        conn, df, date_col=date_col, home_col=home_col, away_col=away_col,
        odds_cols=odds_cols, source=source or path.stem,
    )
