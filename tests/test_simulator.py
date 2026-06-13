import random

from mondial.eval.simulator import (
    MatchOdds, ModelProbs, actual_outcome, pick_per_player, settle, value_pick,
)


def test_player_picks():
    odds = MatchOdds(odd_home=1.5, odd_draw=4.0, odd_away=6.0)
    # Strong draw edge: model_D=0.35 vs book's de-vigged ~0.23 -> +12pp, clears
    # the default min_prob_edge guard. The Quant (model_full) backs the draw on
    # that value; the Purist (model_fundamental) ignores the market and backs the
    # single most-likely outcome (argmax = H). The two diverge by design.
    probs = ModelProbs(p_home=0.50, p_draw=0.35, p_away=0.15)
    picks = pick_per_player(odds, probs, probs, random.Random(42))
    assert picks["safe"] == "H"
    assert picks["risk"] == "A"
    assert picks["tide"] == "D"
    assert probs.argmax() == "H"
    assert picks["model_full"] == "D"            # value vs market
    assert picks["model_fundamental"] == "H"     # pure argmax, ignores odds
    assert picks["model_full"] != picks["model_fundamental"]
    assert picks["monkey"] in ("H", "D", "A")


def test_value_pick_guard():
    odds = MatchOdds(odd_home=1.5, odd_draw=4.0, odd_away=6.0)
    # Weak draw edge: EV picks D (0.25*4.0=1.0 > 0.6*1.5=0.9) but model_D=0.25 is
    # only ~+2pp over the book's ~0.23 implied -> below the default guard, so the
    # picker reverts to the market favorite (H). Unguarded (tau=0) it flips to D.
    weak = ModelProbs(p_home=0.60, p_draw=0.25, p_away=0.15)
    assert value_pick(weak, odds) == "H"            # default guard reverts the flip
    assert value_pick(weak, odds, 0.0) == "D"       # pure-EV (old behaviour)
    # Strong draw edge clears the guard -> keeps the contrarian draw.
    strong = ModelProbs(p_home=0.50, p_draw=0.35, p_away=0.15)
    assert value_pick(strong, odds) == "D"
    # Short home odds with a near-certain home win -> candidate IS the favorite,
    # guard never engages, value agrees with argmax.
    sure = ModelProbs(p_home=0.9, p_draw=0.07, p_away=0.03)
    assert value_pick(sure, odds) == "H" == sure.argmax()


def test_settle_win_loss():
    assert settle("H", "H", odd=2.0) == 20.0
    assert settle("H", "A", odd=2.0) == 0.0


def test_actual_outcome():
    assert actual_outcome(2, 1) == "H"
    assert actual_outcome(1, 1) == "D"
    assert actual_outcome(0, 1) == "A"
