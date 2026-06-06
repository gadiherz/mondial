"""Leakage-free streaming rating + momentum backfill.

Single date-ordered pass over every final match. For each match, BEFORE applying
the Glicko-2 update, we record each team's *pre-match* state -- rating, RD, and
two time-decayed momentum accumulators -- into `match_features`. Because every
recorded quantity depends only on matches strictly earlier than the current one,
the resulting rows are safe to use directly as training features (no look-ahead).

Momentum signals (the black-swan machinery; see the design discussion and
glicko.py's note on why we do NOT use Glicko volatility):

  * resid_ewma -- exponentially-weighted mean of (actual result - Glicko
    expectation), per team. Positive => the team has been beating expectation
    lately => rising/underrated. This is *ranking-orthogonal by construction*:
    it is the part of recent results the rating did not predict. Defined
    zero-sum per match (away residual = -home residual).
  * gd_ewma -- exponentially-weighted mean recent goal difference (capped),
    a raw recent-form signal.

Both use a time-based half-life so a result 270 days ago counts half as much as a
result today, matching the sparse, bursty international calendar. The EWMA is
stored as (numerator, denominator); the mean num/den is gap-invariant, so the
pre-match read needs no extra decay (staleness is captured separately by
idle_days).

After the pass, the latest per-team snapshot is written to `team_state` so the
prediction path can build features for upcoming (scheduled) matches.
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import date

from mondial.model import glicko

log = logging.getLogger("ratings")

RESID_HALFLIFE_DAYS = 270.0   # momentum memory ~9 months; tuned in backtest
GD_HALFLIFE_DAYS = 270.0
GD_CAP = 4                     # clip goal difference to limit blowout influence


def _days(a: str, b: str) -> float:
    return (date.fromisoformat(a) - date.fromisoformat(b)).days


@dataclass
class Momentum:
    """Two time-decayed EWMA accumulators for one team (residual + goal diff)."""
    num_r: float = 0.0
    den_r: float = 0.0
    num_g: float = 0.0
    den_g: float = 0.0
    last_date: str | None = None

    def value(self) -> tuple[float, float]:
        resid = self.num_r / self.den_r if self.den_r > 0 else 0.0
        gd = self.num_g / self.den_g if self.den_g > 0 else 0.0
        return resid, gd

    def observe(
        self, when: str, resid: float, gd: float,
        resid_hl: float, gd_hl: float,
    ) -> None:
        if self.last_date is not None:
            gap = max(_days(when, self.last_date), 0.0)
            fr = 0.5 ** (gap / resid_hl)
            fg = 0.5 ** (gap / gd_hl)
        else:
            fr = fg = 1.0
        self.num_r = self.num_r * fr + resid
        self.den_r = self.den_r * fr + 1.0
        self.num_g = self.num_g * fg + gd
        self.den_g = self.den_g * fg + 1.0
        self.last_date = when


@dataclass
class _State:
    ratings: dict[int, glicko.Rating] = field(default_factory=dict)
    momentum: dict[int, Momentum] = field(default_factory=dict)
    last_played: dict[int, str] = field(default_factory=dict)

    def rating(self, tid: int) -> glicko.Rating:
        return self.ratings.get(tid, glicko.Rating())

    def mom(self, tid: int) -> Momentum:
        m = self.momentum.get(tid)
        if m is None:
            m = self.momentum[tid] = Momentum()
        return m


def stream_backfill(
    conn: sqlite3.Connection,
    *,
    resid_halflife_days: float = RESID_HALFLIFE_DAYS,
    gd_halflife_days: float = GD_HALFLIFE_DAYS,
) -> int:
    """Run the leakage-free pass; populate match_features and team_state.

    Returns the number of match_features rows written. Idempotent: existing rows
    are replaced (INSERT OR REPLACE), team_state is upserted.
    """
    rows = conn.execute(
        """SELECT match_id, date, home_id, away_id, home_goals, away_goals,
                  tournament, neutral
           FROM matches
           WHERE status='final'
             AND home_goals IS NOT NULL AND away_goals IS NOT NULL
           ORDER BY date, match_id"""
    ).fetchall()

    st = _State()
    written = 0
    for r in rows:
        h, a = r["home_id"], r["away_id"]
        rh, ra = st.rating(h), st.rating(a)
        neutral = bool(r["neutral"])

        h_idle = _days(r["date"], st.last_played[h]) if h in st.last_played else 0.0
        a_idle = _days(r["date"], st.last_played[a]) if a in st.last_played else 0.0

        # --- record PRE-match feature row (everything below is pre-update) ---
        mh_resid, mh_gd = st.mom(h).value()
        ma_resid, ma_gd = st.mom(a).value()
        conn.execute(
            """INSERT OR REPLACE INTO match_features
               (match_id, date, home_glicko, away_glicko, home_rd, away_rd,
                home_resid_ewma, away_resid_ewma, home_gd_ewma, away_gd_ewma,
                home_idle_days, away_idle_days)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (r["match_id"], r["date"], rh.rating, ra.rating, rh.rd, ra.rd,
             mh_resid, ma_resid, mh_gd, ma_gd, h_idle, a_idle),
        )
        written += 1

        # --- compute this match's residual / goal diff, then update state ---
        hg, ag = int(r["home_goals"]), int(r["away_goals"])
        s_home = 1.0 if hg > ag else 0.5 if hg == ag else 0.0
        e_home = glicko.expected_score(rh, ra, neutral=neutral)
        resid_home = s_home - e_home            # zero-sum: away residual = -home
        gd_home = max(-GD_CAP, min(GD_CAP, hg - ag))

        st.mom(h).observe(r["date"], resid_home, gd_home,
                          resid_halflife_days, gd_halflife_days)
        st.mom(a).observe(r["date"], -resid_home, -gd_home,
                          resid_halflife_days, gd_halflife_days)

        upd = glicko.update(
            home=rh, away=ra, home_goals=hg, away_goals=ag,
            tournament=r["tournament"], neutral=neutral,
            home_idle_days=h_idle, away_idle_days=a_idle,
        )
        st.ratings[h], st.ratings[a] = upd.new_home, upd.new_away
        st.last_played[h] = st.last_played[a] = r["date"]

    # --- persist latest per-team snapshot for predicting scheduled matches ---
    as_of = rows[-1]["date"] if rows else date.today().isoformat()
    for tid, rt in st.ratings.items():
        resid, gd = st.mom(tid).value()
        conn.execute(
            """INSERT INTO team_state
               (team_id, as_of_date, glicko, rd, vol, resid_ewma, gd_ewma,
                last_match_date)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(team_id) DO UPDATE SET
                   as_of_date=excluded.as_of_date, glicko=excluded.glicko,
                   rd=excluded.rd, vol=excluded.vol,
                   resid_ewma=excluded.resid_ewma, gd_ewma=excluded.gd_ewma,
                   last_match_date=excluded.last_match_date""",
            (tid, as_of, rt.rating, rt.rd, rt.vol, resid, gd,
             st.last_played.get(tid)),
        )

    log.info("stream_backfill: wrote %d match_features rows, %d team_state rows",
             written, len(st.ratings))
    return written
