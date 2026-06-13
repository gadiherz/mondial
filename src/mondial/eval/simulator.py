"""Six-player virtual betting simulator. See ARCHITECTURE.md §7.

Each player stakes a flat 10 NIS per match (no compounding in V1).
"""
from __future__ import annotations

import random
from dataclasses import dataclass

from mondial.config import MIN_PROB_EDGE

STAKE_NIS = 10.0
OUTCOMES = ("H", "D", "A")


@dataclass(frozen=True)
class MatchOdds:
    odd_home: float
    odd_draw: float
    odd_away: float

    def by_outcome(self, o: str) -> float:
        return {"H": self.odd_home, "D": self.odd_draw, "A": self.odd_away}[o]

    def lowest(self) -> str:
        return min(OUTCOMES, key=self.by_outcome)

    def highest(self) -> str:
        return max(OUTCOMES, key=self.by_outcome)


@dataclass(frozen=True)
class ModelProbs:
    p_home: float
    p_draw: float
    p_away: float

    def by_outcome(self, o: str) -> float:
        return {"H": self.p_home, "D": self.p_draw, "A": self.p_away}[o]

    def argmax(self) -> str:
        return OUTCOMES[
            [self.p_home, self.p_draw, self.p_away].index(
                max(self.p_home, self.p_draw, self.p_away)
            )
        ]


def _book_probs(odds: MatchOdds) -> dict[str, float]:
    """De-vigged (margin-free) implied probability per outcome from decimal odds.

    Mirrors eval/odds.devig but inlined to keep simulator import-free of odds.py
    (odds.py imports MatchOdds from here, so importing back would cycle).
    """
    raw = {o: 1.0 / odds.by_outcome(o) for o in OUTCOMES}
    s = sum(raw.values())
    return {o: raw[o] / s for o in OUTCOMES}


def value_pick(probs: ModelProbs, odds: MatchOdds,
               min_prob_edge: float = MIN_PROB_EDGE) -> str:
    """Expected-value pick WITH a min-edge guard before deserting the favorite.

    The candidate is argmax_o (p_o * odd_o): the outcome whose price most
    over-rewards the model's probability. This is what lets the model back a draw
    or an underdog instead of the market favorite -- argmax-probability almost
    never selects the draw, so it would cede every X to the `tide` player.

    But the unguarded rule flips on ANY sliver of edge and so chases long-odds
    outcomes on noise. The guard: when the candidate is NOT the market favorite
    (lowest odd), keep it only if the model's probability exceeds the book's
    margin-free implied probability by at least `min_prob_edge` probability points
    -- i.e. the deviation from the price is strong enough; otherwise fall back to
    the favorite. `min_prob_edge <= 0` restores the pure-EV behaviour. We measure
    the gap in probability space (not EV) on purpose: a tiny absolute prob bump on
    a longshot is a huge EV edge, so an EV gate would still chase longshots.
    Calibrate `min_prob_edge` with scripts/sweep_min_edge.py (see ARCHITECTURE §7.1).

    For a fixed 1X2 coupon (TOTO WINNER 16) we always pick one of the three; "no
    bet when no positive edge" is moot because the coupon must be filled -- the
    guard's fallback is the favorite, never an abstention.

    REQUIRES real odds. With no odds source wired (odds_snapshots is empty) the
    model players cannot value-bet WC fixtures yet -- there is no price to value
    against. This is a hard dependency on the Odds API scraper.
    """
    candidate = max(OUTCOMES, key=lambda o: probs.by_outcome(o) * odds.by_outcome(o))
    favorite = odds.lowest()
    if candidate == favorite or min_prob_edge <= 0.0:
        return candidate
    prob_edge = probs.by_outcome(candidate) - _book_probs(odds)[candidate]
    return candidate if prob_edge >= min_prob_edge else favorite


def pick_per_player(
    odds: MatchOdds,
    model_full: ModelProbs,
    model_fundamental: ModelProbs,
    rng: random.Random,
    min_prob_edge: float = MIN_PROB_EDGE,
) -> dict[str, str]:
    return {
        # The Quant: bet the outcome whose price most over-rewards the model.
        "model_full": value_pick(model_full, odds, min_prob_edge),
        # The Purist: bet the model's single most-likely outcome, ignoring the
        # market entirely (pure argmax probability). Distinct from model_full,
        # which only deserts the favourite when the odds justify it.
        "model_fundamental": model_fundamental.argmax(),
        "safe": odds.lowest(),
        "tide": "D",
        "risk": odds.highest(),
        "monkey": rng.choice(OUTCOMES),
    }


def settle(pick: str, actual: str, odd: float, stake: float = STAKE_NIS) -> float:
    """Return payout (>0 if won, 0 if lost). Profit = payout - stake."""
    return stake * odd if pick == actual else 0.0


def actual_outcome(home_goals: int, away_goals: int) -> str:
    if home_goals > away_goals:
        return "H"
    if home_goals < away_goals:
        return "A"
    return "D"
