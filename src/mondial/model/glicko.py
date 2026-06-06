"""Glicko-2 Bayesian team rating, online-updatable — the momentum/black-swan engine.

Why Glicko-2 over the point-Elo in `elo.py` (see ARCHITECTURE.md §6.2 and the
design discussion): a point rating encodes long-run pedigree and has thin tails,
so an argmax/strongest-team policy always reduces to the "safe better". Glicko-2
carries two extra per-team quantities that directly target the black-swan goal:

  * phi  (rating deviation, RD) -- uncertainty. Integrating the predictive
    distribution over phi *widens* it, giving underdogs more probability mass
    than point-Elo allows. Fat tails = black swans get a fair shot. This is the
    quantity we actively use as a feature.
  * sigma (volatility) -- in principle a momentum signal. EMPIRICAL FINDING
    (scripts/inspect_glicko.py on the real corpus): at international-football
    data density (~1-4 caps/month) sigma stays glued to its ~0.06 prior even
    under monthly rating periods and tau up to 4.0. It is therefore NOT exposed
    as a momentum feature -- momentum is captured instead by the EWMA Elo-
    residual (over/under-performance vs expectation) in features.py, plus the
    Dixon-Coles time-decay. sigma is retained only because Glicko-2 requires it
    internally to govern the RD update (phi* = sqrt(phi^2 + sigma^2)).

Standard reference: Glickman (2013), "Example of the Glicko-2 system"
(www.glicko.net/glicko/glicko2.pdf). We apply it online with a rating period of
a single match, plus three football-specific adaptations:

  1. Goal-margin multiplier  -- a 4-0 carries more evidence than a 1-0. Reuses
     the eloratings goal factor from `elo.goal_factor`.
  2. Tournament importance    -- a friendly should move ratings less than a World
     Cup match. Scales the per-match evidence weight (see IMPORTANCE).
  3. Idle-time RD inflation    -- a team that has not played in months is more
     uncertain. phi grows with elapsed FIFA windows before each update.

All three fold into a single per-team-per-match evidence weight, so the update is
a *weighted* single-game Glicko-2 step (omega games' worth of evidence).
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from mondial.model.elo import goal_factor

# --- Glicko-2 constants ---------------------------------------------------
SCALE = 173.7178            # display <-> Glicko-2 internal scale
DEFAULT_RATING = 1500.0     # display-scale mean for an unseen team
DEFAULT_RD = 350.0          # display-scale deviation for an unseen team
DEFAULT_VOL = 0.06          # initial volatility (Glickman's default)
TAU = 0.5                   # system constant: caps volatility change per step.
#                             Larger -> ratings react faster to surprises.
CONVERGENCE = 1e-6          # tolerance for the volatility solver

# --- Football adaptations -------------------------------------------------
HOME_ADVANTAGE = 65.0       # display-scale edge added to the home team when not
#                             neutral. WC matches are neutral (host excepted).
PERIOD_DAYS = 30.0          # one FIFA match window ~= one rating period, used to
#                             convert idle days into RD-inflation periods.
MAX_IDLE_PERIODS = 12.0     # cap idle inflation at ~1 year of inactivity.

# Per-match evidence weight by tournament category (mirrors the elo.k_factor
# hierarchy, normalised so a major-tournament match = 1.0 game of evidence).
IMPORTANCE: dict[str, float] = {
    "WC": 1.0, "EURO": 1.0, "COPA": 1.0, "AFCON": 1.0, "ASIAN_CUP": 1.0,
    "WC_QUAL": 0.85, "NATIONS": 0.85,
    "FRIENDLY": 0.5,
}


@dataclass
class Rating:
    """A team's Glicko-2 state, stored in human-readable display scale."""
    rating: float = DEFAULT_RATING   # mu, display scale (~1500 baseline)
    rd: float = DEFAULT_RD           # phi, display scale (uncertainty)
    vol: float = DEFAULT_VOL         # sigma (volatility / momentum signal)

    def mu(self) -> float:
        return (self.rating - DEFAULT_RATING) / SCALE

    def phi(self) -> float:
        return self.rd / SCALE


@dataclass
class GlickoUpdate:
    new_home: Rating
    new_away: Rating


def importance(tournament: str) -> float:
    return IMPORTANCE.get(tournament.upper(), 0.5)


def _g(phi: float) -> float:
    return 1.0 / math.sqrt(1.0 + 3.0 * phi * phi / (math.pi * math.pi))


def _E(mu: float, mu_opp: float, phi_opp: float) -> float:
    """Glicko expected score (1=win, 0.5=draw-equivalent) of `mu` vs `mu_opp`."""
    return 1.0 / (1.0 + math.exp(-_g(phi_opp) * (mu - mu_opp)))


def expected_score(
    home: Rating, away: Rating, *, neutral: bool = True
) -> float:
    """Expected score of the home team in [0,1] (0.5 ~ even). Used both as the
    win/draw/loss expectation for residual-momentum features and as a sanity
    probe. Home advantage applies only when not neutral.
    """
    adv = 0.0 if neutral else HOME_ADVANTAGE / SCALE
    return _E(home.mu() + adv, away.mu(), away.phi())


def _idle_periods(idle_days: float) -> float:
    if idle_days <= 0.0:
        return 0.0
    return min(idle_days / PERIOD_DAYS, MAX_IDLE_PERIODS)


def _inflate_phi(phi: float, vol: float, idle_days: float) -> float:
    """RD inflation for elapsed time: phi grows by sigma per rating period of
    inactivity (Glickman's pre-period step, generalised to fractional periods)."""
    periods = _idle_periods(idle_days)
    return math.sqrt(phi * phi + vol * vol * periods)


def _solve_volatility(phi: float, v: float, delta: float, vol: float) -> float:
    """Glickman's Illinois (regula-falsi) iteration for the new volatility."""
    a = math.log(vol * vol)
    d2 = delta * delta
    phi2 = phi * phi

    def f(x: float) -> float:
        ex = math.exp(x)
        num = ex * (d2 - phi2 - v - ex)
        den = 2.0 * (phi2 + v + ex) ** 2
        return num / den - (x - a) / (TAU * TAU)

    A = a
    if d2 > phi2 + v:
        B = math.log(d2 - phi2 - v)
    else:
        k = 1
        while f(a - k * TAU) < 0.0:
            k += 1
        B = a - k * TAU

    fA, fB = f(A), f(B)
    while abs(B - A) > CONVERGENCE:
        C = A + (A - B) * fA / (fB - fA)
        fC = f(C)
        if fC * fB <= 0.0:
            A, fA = B, fB
        else:
            fA /= 2.0
        B, fB = C, fC
    return math.exp(A / 2.0)


def _update_one(
    team: Rating, opp: Rating, score: float, weight: float, idle_days: float,
    *, adv: float = 0.0,
) -> Rating:
    """Single weighted Glicko-2 step for `team` facing one opponent `opp`.

    score is the team's result (1 win / 0.5 draw / 0 loss). weight is the
    evidence multiplier omega (importance * goal margin) -- the match counts as
    `weight` games' worth of information. adv is the display-scale home edge
    already converted by the caller's perspective (added to this team's mu).
    """
    mu = team.mu() + adv / SCALE
    # Inflate this team's RD for the time it has been idle, then proceed.
    phi = _inflate_phi(team.phi(), team.vol, idle_days)

    g_opp = _g(opp.phi())
    E = _E(mu, opp.mu(), opp.phi())
    # Weighted single-game estimation variance and improvement.
    info = weight * g_opp * g_opp * E * (1.0 - E)
    if info <= 0.0:
        # Degenerate (E at 0 or 1, or zero weight): only inflate uncertainty.
        new_phi = phi
        return Rating(team.rating, new_phi * SCALE, team.vol)
    v = 1.0 / info
    delta_sum = weight * g_opp * (score - E)
    delta = v * delta_sum

    new_vol = _solve_volatility(phi, v, delta, team.vol)
    phi_star = math.sqrt(phi * phi + new_vol * new_vol)
    new_phi = 1.0 / math.sqrt(1.0 / (phi_star * phi_star) + 1.0 / v)
    new_mu = mu - adv / SCALE + new_phi * new_phi * delta_sum

    return Rating(
        rating=new_mu * SCALE + DEFAULT_RATING,
        rd=new_phi * SCALE,
        vol=new_vol,
    )


def update(
    *,
    home: Rating,
    away: Rating,
    home_goals: int,
    away_goals: int,
    tournament: str,
    neutral: bool = False,
    home_idle_days: float = 0.0,
    away_idle_days: float = 0.0,
) -> GlickoUpdate:
    """Online Glicko-2 update for one match (rating period = this match).

    Each team is updated treating the other as its single opponent, using a
    shared per-match evidence weight = tournament importance * goal-margin
    factor. Home advantage applies only when not neutral (WC venues are neutral
    except when a host plays at home).
    """
    if home_goals > away_goals:
        s_home, s_away = 1.0, 0.0
    elif home_goals == away_goals:
        s_home = s_away = 0.5
    else:
        s_home, s_away = 0.0, 1.0

    weight = importance(tournament) * goal_factor(home_goals - away_goals)
    adv = 0.0 if neutral else HOME_ADVANTAGE

    new_home = _update_one(home, away, s_home, weight, home_idle_days, adv=adv)
    new_away = _update_one(away, home, s_away, weight, away_idle_days, adv=0.0)
    return GlickoUpdate(new_home=new_home, new_away=new_away)
