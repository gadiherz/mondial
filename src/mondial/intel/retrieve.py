"""Stage 2a: semantic distillation of a scraped corpus down to the passages that
actually bear on individual player/coach PERFORMANCE OUTLOOK.

This is the robust, keyword-free core: every sentence is embedded with a local
ONNX model (fastembed) and scored by cosine similarity to a set of natural-
language ANCHOR concepts describing over/under-performance, emerging players,
declining stars, sharpness, and coaching effect. The top passages within a small
char budget are returned -- so the (paid) LLM step reads dense analysis, not
60k chars of noise, and transfer-gossip / fixture-logistics sentences fall away
because they're semantically far from the anchors.

fastembed is optional (`intel` extra). If absent, distill() returns "" and the
pipeline falls back to the headline path -- a clean degrade, not a crash.
"""
from __future__ import annotations

import logging
import re

log = logging.getLogger("intel.retrieve")

# Natural-language anchors, NOT keywords -- the embedder generalises across all
# the ways a journalist phrases these. Extend/curate by inspecting selections.
_ANCHORS = [
    "a player is in superb form, exceeding expectations, the standout performer",
    "a star player is underperforming, off the pace, anonymous, below his usual level",
    "a young or unheralded player has been the revelation, impressing everyone",
    "an ageing or declining star no longer brings his usual game",
    "a key player looks sharp, fit and dangerous in training and warm-up matches",
    "a key player is a doubt, lacking match sharpness or struggling for fitness",
    "the coach's tactics and team selection have lifted or stifled the team",
    "the squad has momentum, confidence and unity, or is in crisis and disarray",
    "a player is carrying the team, the talisman they depend on",
    "internal disputes, morale problems, or a player rift in the camp",
]

# Anti-anchors: topics we want to EXCLUDE. A sentence's final score subtracts its
# similarity to these, so transfer gossip / club business is pushed out by meaning
# even though it names the same players. This is the semantic noise filter.
_ANTI_ANCHORS = [
    "transfer speculation: a player wanted by clubs, a summer move, a signing target",
    "contract talks, transfer fee, release clause, available on a free transfer",
    "club business unrelated to the national team: manager vacancies, finances, kit launch, ticket sales",
    "match schedule, fixtures, kick-off times, qualification results and group standings",
]
_ANTI_WEIGHT = 0.7  # how hard to penalise off-topic similarity (tunable)

_SENT = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"'])")

_model = None
_anchor_emb = None
_anti_emb = None


def _get_model():
    global _model
    if _model is None:
        from fastembed import TextEmbedding  # optional dep
        _model = TextEmbedding()
    return _model


def _embed(texts: list[str]):
    import numpy as np
    return np.array(list(_get_model().embed(texts)), dtype=float)


def _anchors():
    global _anchor_emb, _anti_emb
    if _anchor_emb is None:
        import numpy as np
        a = _embed(_ANCHORS)
        b = _embed(_ANTI_ANCHORS)
        _anchor_emb = a / np.linalg.norm(a, axis=1, keepdims=True)
        _anti_emb = b / np.linalg.norm(b, axis=1, keepdims=True)
    return _anchor_emb, _anti_emb


def _sentences(text: str) -> list[str]:
    out = []
    for para in text.split("\n"):
        for s in _SENT.split(para):
            s = s.strip()
            if 40 <= len(s) <= 400:
                out.append(s)
    return out


def distill(
    texts: list[str], *, ground: set[str] | None = None, budget_chars: int = 4000,
    min_score: float = 0.10, max_sentences: int = 500,
) -> str:
    """Return the highest performance-relevance passages, within `budget_chars`.

    `ground` (lowercased player/coach/team terms) filters to sentences about THIS
    team's people BEFORE semantic ranking -- this is what stops the retriever
    surfacing analysis about other teams in a noisy corpus. Empty string if
    fastembed is unavailable or nothing qualifies (caller falls back to headlines).
    """
    if not texts:
        return ""
    try:
        import numpy as np
        _get_model()
    except ImportError:
        log.warning("fastembed not installed; deep distillation unavailable "
                    "(falling back to headlines). Install the `intel` extra.")
        return ""

    sents: list[str] = []
    seen: set[str] = set()
    for t in texts:
        for s in _sentences(t):
            key = s[:80].lower()
            if key not in seen:
                seen.add(key)
                sents.append(s)
    if ground:
        useful = [t for t in ground if len(t) >= 4]
        if useful:
            pat = re.compile(r"\b(" + "|".join(re.escape(t) for t in
                             sorted(useful, key=len, reverse=True)) + r")\b",
                             re.IGNORECASE)
            sents = [s for s in sents if pat.search(s)]
    if not sents:
        return ""
    sents = sents[:max_sentences]

    An, Anti = _anchors()
    S = _embed(sents)
    Sn = S / np.linalg.norm(S, axis=1, keepdims=True)
    perf = (Sn @ An.T).max(axis=1)          # closeness to performance topics
    anti = (Sn @ Anti.T).max(axis=1)        # closeness to transfer/club-business
    score = perf - _ANTI_WEIGHT * anti      # contrastive: reward perf, punish noise

    picked: list[str] = []
    total = 0
    for i in np.argsort(-score):
        if score[i] < min_score:
            break
        s = sents[i]
        picked.append(s)
        total += len(s) + 1
        if total >= budget_chars:
            break
    log.info("retrieve: %d sentences -> kept %d (%d chars, top contrastive %.2f)",
             len(sents), len(picked), total, float(score.max()) if len(sents) else 0.0)
    return "\n".join(picked)
