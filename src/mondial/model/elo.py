"""World Football Elo Ratings, online-updatable.

Spec (eloratings.net):
    E_home = 1 / (1 + 10^((R_away - R_home - 100) / 400))   # 100 = home advantage
    R'     = R + K * G * (W - E)
where:
    K = 60 for World Cup finals, 50 for continental finals,
        40 for qualifiers, 30 for friendlies.
    G = 1 (1-goal margin) ; 1.5 (2-goal) ; (11 + |gd|) / 8 (3+)
    W = 1 win / 0.5 draw / 0 loss
"""
from __future__ import annotations

from dataclasses import dataclass

DEFAULT_RATING = 1500.0
HOME_ADVANTAGE = 100.0


@dataclass
class EloUpdate:
    new_home: float
    new_away: float


def expected(home: float, away: float, neutral: bool = False) -> float:
    adv = 0.0 if neutral else HOME_ADVANTAGE
    return 1.0 / (1.0 + 10 ** ((away - home - adv) / 400.0))


def goal_factor(gd: int) -> float:
    gd = abs(gd)
    if gd <= 1:
        return 1.0
    if gd == 2:
        return 1.5
    return (11 + gd) / 8.0


def k_factor(tournament: str) -> int:
    return {
        "WC": 60, "EURO": 50, "COPA": 50, "AFCON": 50, "ASIAN_CUP": 50,
        "WC_QUAL": 40, "NATIONS": 40,
        "FRIENDLY": 30,
    }.get(tournament.upper(), 30)


def update(
    *, home: float, away: float, home_goals: int, away_goals: int,
    tournament: str, neutral: bool = False,
) -> EloUpdate:
    E = expected(home, away, neutral=neutral)
    W = 1.0 if home_goals > away_goals else 0.5 if home_goals == away_goals else 0.0
    K = k_factor(tournament)
    G = goal_factor(home_goals - away_goals)
    delta = K * G * (W - E)
    return EloUpdate(new_home=home + delta, new_away=away - delta)
