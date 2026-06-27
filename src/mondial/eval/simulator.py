"""Six-player virtual betting simulator. See ARCHITECTURE.md §7.

Each player stakes a flat 10 NIS per match (no compounding in V1).
"""
from __future__ import annotations

import random
from dataclasses import dataclass

from mondial.config import DRAW_MIN_PROB_EDGE, DRAW_THRESHOLD, MIN_PROB_EDGE

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

    def argmax_draw_aware(self, draw_threshold: float = DRAW_THRESHOLD) -> str:
        """Most-likely outcome, but able to actually pick the draw.

        Plain argmax almost never selects D: the draw is rarely the single highest
        of the three even when it is a genuinely likely (~25-30%) outcome, so a
        pure-argmax player picks D ~0% of the time and cedes every draw. This picks
        D when its (calibrated) probability clears `draw_threshold` -- a base-rate-
        tuned cutoff below 1/3, so a strong-but-not-modal draw is still backed --
        and otherwise the more likely of H/A. A high `draw_threshold` (>= 1) means
        the draw is never picked (it reduces to argmax over {H, A}). High p_draw only
        co-occurs with a balanced H/A field (a 0.70 favourite leaves no room for
        p_draw >= 0.30), so this never backs a dominated draw.
        """
        if self.p_draw >= draw_threshold:
            return "D"
        return "H" if self.p_home >= self.p_away else "A"


def _book_probs(odds: MatchOdds) -> dict[str, float]:
    """De-vigged (margin-free) implied probability per outcome from decimal odds.

    Mirrors eval/odds.devig but inlined to keep simulator import-free of odds.py
    (odds.py imports MatchOdds from here, so importing back would cycle).
    """
    raw = {o: 1.0 / odds.by_outcome(o) for o in OUTCOMES}
    s = sum(raw.values())
    return {o: raw[o] / s for o in OUTCOMES}


def value_pick(probs: ModelProbs, odds: MatchOdds,
               min_prob_edge: float = MIN_PROB_EDGE,
               draw_min_prob_edge: float = DRAW_MIN_PROB_EDGE) -> str:
    """Expected-value pick WITH a min-edge guard before deserting the favorite.

    The candidate is argmax_o (p_o * odd_o): the outcome whose price most
    over-rewards the model's probability. This is what lets the model back a draw
    or an underdog instead of the market favorite -- argmax-probability almost
    never selects the draw, so it would cede every X to the `tide` player.

    But the unguarded rule flips on ANY sliver of edge and so chases long-odds
    outcomes on noise. The guard: when the candidate is NOT the market favorite
    (lowest odd), keep it only if the model's probability exceeds the book's
    margin-free implied probability by at least an edge floor -- i.e. the deviation
    from the price is strong enough; otherwise fall back to the favorite. We measure
    the gap in probability space (not EV) on purpose: a tiny absolute prob bump on
    a longshot is a huge EV edge, so an EV gate would still chase longshots.

    The DRAW gets a smaller floor (`draw_min_prob_edge` <= `min_prob_edge`): the
    longshot-chasing the main guard exists to stop is an underdog-WIN problem, and
    holding draws to the same high bar is what kept the model picking D only ~4% of
    the time vs a ~25-30% real draw rate. The floor used for a draw is
    min(min_prob_edge, draw_min_prob_edge) so an explicit `min_prob_edge <= 0`
    (pure-EV) still applies to draws too. Calibrate both with scripts/sweep_draw.py
    (draw) and scripts/sweep_min_edge.py (underdog); see ARCHITECTURE §7.1.

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
    edge_floor = (min(min_prob_edge, draw_min_prob_edge) if candidate == "D"
                  else min_prob_edge)
    prob_edge = probs.by_outcome(candidate) - _book_probs(odds)[candidate]
    return candidate if prob_edge >= edge_floor else favorite


def pick_per_player(
    odds: MatchOdds,
    model_full: ModelProbs,
    model_fundamental: ModelProbs,
    rng: random.Random,
    min_prob_edge: float = MIN_PROB_EDGE,
) -> dict[str, str]:
    return {
        # The Quant: bet the outcome whose price most over-rewards the model
        # (with the draw-relaxed edge floor so it can back value draws).
        "model_full": value_pick(model_full, odds, min_prob_edge),
        # The Purist: bet the model's most-likely outcome, ignoring the market --
        # but draw-aware (argmax_draw_aware) so a genuinely likely draw is backed
        # instead of always ceding it. Distinct from model_full, which deserts the
        # favourite only when the odds justify it.
        "model_fundamental": model_fundamental.argmax_draw_aware(),
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
