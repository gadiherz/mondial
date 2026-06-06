"""Orchestrate the 3-stage intel pipeline for one team and return its vector.

  gather_team_context (free sources)  ->  score_team (hosted LLM)  ->  IntelVector

The vector's signed dimensions are averaged and scaled by confidence into a
single `composite` in [-1, 1], which the prediction path turns into a Glicko
rating nudge (model/features.py).

LEAKAGE / VALIDATION NOTE: the sources are CURRENT text, valid only for FUTURE
matches (no result exists yet) and NOT backtestable. Wired by explicit decision;
validate forward against live results.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from mondial.intel.processor import score_team
from mondial.intel.sources import gather_team_context

log = logging.getLogger("intel.llm")

# Composite weighting: form_outlook (the black-swan signal) dominates, injuries
# second, the rest minor. Tunable. Weights sum to 1 so composite stays in [-1,1].
_W = {"form_outlook": 0.45, "availability": 0.25, "coach_stability": 0.10,
      "morale": 0.10, "motivation": 0.10}


def _clip(x, lo: float, hi: float) -> float:
    try:
        return max(lo, min(hi, float(x)))
    except (TypeError, ValueError):
        return 0.0


@dataclass
class IntelVector:
    form_outlook: float
    availability: float
    coach_stability: float
    morale: float
    motivation: float
    confidence: float
    summary: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "IntelVector":
        return cls(
            form_outlook=_clip(d.get("form_outlook", 0.0), -1, 1),
            availability=_clip(d.get("availability", 0.0), -1, 1),
            coach_stability=_clip(d.get("coach_stability", 0.0), -1, 1),
            morale=_clip(d.get("morale", 0.0), -1, 1),
            motivation=_clip(d.get("motivation", 0.0), -1, 1),
            confidence=_clip(d.get("confidence", 0.0), 0, 1),
            summary=str(d.get("summary", ""))[:300],
        )

    def composite(self) -> float:
        """Signed value in [-1, 1] driving the rating nudge: form-weighted mean of
        the signed dimensions, scaled by confidence (uncertain reading -> small)."""
        signed = (_W["form_outlook"] * self.form_outlook
                  + _W["availability"] * self.availability
                  + _W["coach_stability"] * self.coach_stability
                  + _W["morale"] * self.morale
                  + _W["motivation"] * self.motivation)
        return _clip(signed, -1, 1) * self.confidence


def _deep_context(team_name: str) -> str:
    """Deep path: fetch full articles from the API providers, ground to the
    team's people, distill the performance passages, and append structured
    injury facts verbatim. Returns "" if nothing is available (caller falls back
    to headlines)."""
    from mondial.intel import providers
    from mondial.intel.retrieve import distill
    from mondial.intel.roster import get_ground_terms

    articles = providers.gather_articles(team_name)
    distilled = distill(articles, ground=get_ground_terms(team_name)) if articles else ""
    injuries = providers.injuries_block(team_name)   # already-dense facts, bypass distill
    parts = [p for p in (distilled, injuries) if p]
    return "\n\n".join(parts)


def fetch_team_intel(
    team_name: str, as_of_date: str, *, model: str | None = None,
) -> IntelVector | None:
    """Run source -> processor for one team; return IntelVector or None.

    Prefers the DEEP path (full-article scrape + semantic distillation); falls
    back to the headline path if the deep deps/sources are unavailable. None
    (logged, never a silent zero) when both yield nothing or the LLM fails.
    """
    context = _deep_context(team_name)
    if context:
        log.info("intel(%s): deep distilled %d chars", team_name, len(context))
    else:
        context = gather_team_context(team_name)  # headline fallback
        if context:
            log.info("intel(%s): headline fallback (%d chars)", team_name, len(context))
    parsed = score_team(team_name, as_of_date, context, model=model)
    if parsed is None:
        return None
    return IntelVector.from_dict(parsed)
