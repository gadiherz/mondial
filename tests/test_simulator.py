import random

from mondial.eval.simulator import (
    MatchOdds, ModelProbs, actual_outcome, pick_per_player, settle, value_pick,
)


def test_player_picks():
    odds = MatchOdds(odd_home=1.5, odd_draw=4.0, odd_away=6.0)
    # Strong draw (p_draw=0.35 >= DRAW_THRESHOLD 0.32, and a +12pp value edge over the
    # book's ~0.23). The Quant (model_full) backs it on value; the draw-aware Purist
    # (model_fundamental) backs it too -- plain argmax would say H (the single max),
    # which is exactly the behaviour the draw-aware rule replaces so draws aren't ceded.
    probs = ModelProbs(p_home=0.50, p_draw=0.35, p_away=0.15)
    picks = pick_per_player(odds, probs, probs, random.Random(42))
    assert picks["safe"] == "H"
    assert picks["risk"] == "A"
    assert picks["tide"] == "D"
    assert probs.argmax() == "H"                  # plain argmax still says H...
    assert probs.argmax_draw_aware() == "D"       # ...but the draw-aware rule picks D
    assert picks["model_full"] == "D"             # value vs market
    assert picks["model_fundamental"] == "D"      # draw-aware argmax, ignores odds
    assert picks["monkey"] in ("H", "D", "A")


def test_argmax_draw_aware():
    # Below threshold: defer to the more-likely of H/A, never the draw.
    p = ModelProbs(p_home=0.45, p_draw=0.28, p_away=0.27)
    assert p.argmax() == "H"
    assert p.argmax_draw_aware(0.32) == "H"        # 0.28 < 0.32
    assert p.argmax_draw_aware(0.27) == "D"        # now 0.28 >= 0.27 -> draw
    # A dominant favourite leaves no room for a high draw prob, so a draw is never
    # backed over it (p_draw can't reach the threshold here).
    sure = ModelProbs(p_home=0.80, p_draw=0.13, p_away=0.07)
    assert sure.argmax_draw_aware(0.32) == "H"
    # A clearly modal draw clears the production threshold -> picked.
    tight = ModelProbs(p_home=0.34, p_draw=0.36, p_away=0.30)
    assert tight.argmax() == "D"
    assert tight.argmax_draw_aware(0.32) == "D"
    # A high threshold (>=1) means the draw is never picked -> argmax over {H, A},
    # even when the draw is the global mode.
    assert tight.argmax_draw_aware(1.0) == "H"


def test_value_pick_guard():
    odds = MatchOdds(odd_home=1.5, odd_draw=4.0, odd_away=6.0)
    # Very weak draw edge: EV picks D (0.25*4.0=1.0 > 0.6*1.5=0.9) but model_D=0.25 is
    # only ~+1.9pp over the book's ~0.231 implied -> below even the relaxed draw floor
    # (0.02), so the picker reverts to the market favorite (H). Pure-EV (tau=0) flips D.
    weak = ModelProbs(p_home=0.60, p_draw=0.25, p_away=0.15)
    assert value_pick(weak, odds) == "H"            # below the draw floor -> revert
    assert value_pick(weak, odds, 0.0) == "D"       # pure-EV (min_prob_edge<=0)
    # Strong draw edge clears the guard -> keeps the contrarian draw.
    strong = ModelProbs(p_home=0.50, p_draw=0.35, p_away=0.15)
    assert value_pick(strong, odds) == "D"
    # Short home odds with a near-certain home win -> candidate IS the favorite,
    # guard never engages, value agrees with argmax.
    sure = ModelProbs(p_home=0.9, p_draw=0.07, p_away=0.03)
    assert value_pick(sure, odds) == "H" == sure.argmax()


def test_value_pick_draw_floor_is_relaxed():
    # A moderate value-draw whose edge sits BETWEEN the relaxed draw floor (0.02) and
    # the underdog floor (0.08): kept as a draw now, but would have been reverted to
    # the favorite under the old single-floor rule. Same edge on an AWAY underdog is
    # still reverted (the underdog floor is unchanged) -- the relaxation is draw-only.
    odds = MatchOdds(odd_home=1.5, odd_draw=4.0, odd_away=6.0)
    # book de-vigged ~ H .615 / D .231 / A .154. EV candidate is D (0.30*4.0=1.20 >
    # 0.55*1.5=0.825 > 0.15*6.0=0.90)... A=0.90, so D wins. model_D=0.30 -> +6.9pp
    # edge: above the 0.02 draw floor, below the 0.08 underdog floor.
    draw = ModelProbs(p_home=0.55, p_draw=0.30, p_away=0.15)
    assert value_pick(draw, odds, 0.08, 0.02) == "D"     # relaxed draw floor keeps it
    assert value_pick(draw, odds, 0.08, 0.08) == "H"     # same floor as underdog -> revert


def test_settle_win_loss():
    assert settle("H", "H", odd=2.0) == 20.0
    assert settle("H", "A", odd=2.0) == 0.0


def test_actual_outcome():
    assert actual_outcome(2, 1) == "H"
    assert actual_outcome(1, 1) == "D"
    assert actual_outcome(0, 1) == "A"
