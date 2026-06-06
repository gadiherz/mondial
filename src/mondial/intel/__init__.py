"""Track B: autonomous LLM-derived team "intel" vectors.

`llm_intel` runs a free, deploy-and-forget RAG pipeline per national team:
full-text news from APIs (Guardian / World News / API-Football injuries,
`providers.py`) -> Wikipedia roster grounding (`roster.py`) -> local fastembed
contrastive semantic distillation (`retrieve.py`) -> a hosted LLM over any
OpenAI-compatible endpoint (`processor.py`; Groq `llama-3.3-70b` by default,
swappable by config) that scores the passages into a numeric performance vector.
`collect` is the autonomous logger that fans this over the upcoming fixtures and
writes `team_intel`. The prediction path (model/features.py) reads `composite`
and nudges the team's effective Glicko rating.
"""
from mondial.intel.llm_intel import IntelVector, fetch_team_intel

__all__ = ["IntelVector", "fetch_team_intel"]
