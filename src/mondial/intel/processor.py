"""Stage 2: turn fetched source text into a numeric intel vector via a hosted LLM.

Talks to any OpenAI-compatible `/chat/completions` endpoint (Groq by default;
Gemini's OpenAI-compat endpoint or a local Ollama by changing `intel_base_url`),
so the provider swaps by config alone -- no code change. Uses plain `requests`
(already a dependency); no provider SDK, and deliberately NOT the Anthropic SDK.

The model scores ONLY from the supplied sources (it cannot browse), and is told
to keep confidence low when the sources are thin.
"""
from __future__ import annotations

import json
import logging
import re
import time

import requests

from mondial.config import settings

log = logging.getLogger("intel.processor")

# Live budget, read from the provider's own rate-limit headers after each call.
# This is the ground truth the run-level guard (collect.py) stops on, so we
# defer teams BEFORE blowing a per-day limit rather than 429-skipping them.
_state: dict[str, float | bool | None] = {
    "remaining_requests": None, "remaining_tokens": None, "exhausted": False,
}


def reset_budget_state() -> None:
    _state.update(remaining_requests=None, remaining_tokens=None, exhausted=False)


def remaining_requests() -> float | None:
    return _state["remaining_requests"]  # type: ignore[return-value]


def remaining_tokens() -> float | None:
    return _state["remaining_tokens"]  # type: ignore[return-value]


def is_exhausted() -> bool:
    return bool(_state["exhausted"])


def _note_headers(resp: requests.Response) -> None:
    for hdr, key in (("x-ratelimit-remaining-requests", "remaining_requests"),
                     ("x-ratelimit-remaining-tokens", "remaining_tokens")):
        raw = resp.headers.get(hdr)
        if raw is not None:
            try:
                _state[key] = float(raw)
            except ValueError:
                pass

_RUBRIC = """You are a football analyst. Using ONLY the provided passages (curated
analyst commentary about a national team's players and coach), output a numeric
intel vector. Do not rely on outside knowledge beyond what the passages support;
if they are sparse, generic, or only about transfers/kit/fixtures, keep
`confidence` LOW and the scores near 0.

Return a single JSON object with these fields. Each score is a float in
[-1.0, 1.0] where 0 = neutral / no signal and POSITIVE = favourable to the team:
  form_outlook     THE KEY BLACK-SWAN SIGNAL. Net individual-performance outlook
                   RELATIVE TO EXPECTATIONS: +1 = emerging/underrated players
                   rising to the occasion and key men in the form of their lives;
                   -1 = stars looking off the pace / below their usual level. Not
                   raw squad strength -- the over/under-performance surprise.
  availability     +1 full-strength squad ... -1 several key players injured/suspended/out
  coach_stability  +1 settled, experienced coach ... -1 interim/new/under pressure/just sacked
  morale           +1 winning run, unity, confidence ... -1 crisis, infighting, poor form
  motivation       +1 high stakes / must-win ... -1 little at stake
  confidence       float in [0.0, 1.0] -- how much concrete, recent, performance-relevant evidence the passages gave
  summary          string <= 300 chars -- the few facts that drove the scores

Output ONLY the JSON object: no prose, no markdown, no code fences."""

_JSON_OBJ = re.compile(r"\{.*\}", re.DOTALL)


def _extract_json(text: str) -> dict | None:
    """Parse the model's reply into a dict, tolerating fences / stray prose."""
    candidates = [text]
    m = _JSON_OBJ.search(text or "")
    if m:
        candidates.append(m.group(0))
    for candidate in candidates:
        try:
            obj = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(obj, dict):
            return obj
    return None


def score_team(team_name: str, as_of_date: str, context: str,
               *, model: str | None = None, timeout: float = 60.0) -> dict | None:
    """Send (rubric + sources) to the hosted LLM; return the parsed JSON dict.

    Returns None (logged, never a silent zero) if no key/endpoint is configured
    or the call/parse fails -- the caller skips that team.
    """
    if not settings.intel_api_key and "localhost" not in settings.intel_base_url:
        log.warning("INTEL_API_KEY not set; skipping intel for %s.", team_name)
        return None
    if not context.strip():
        log.warning("no source text for %s; skipping (would be pure guess).", team_name)
        return None

    url = settings.intel_base_url.rstrip("/") + "/chat/completions"
    headers = {"Content-Type": "application/json"}
    if settings.intel_api_key:
        headers["Authorization"] = f"Bearer {settings.intel_api_key}"
    body = {
        "model": model or settings.intel_model,
        "temperature": 0,
        "max_tokens": 700,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": _RUBRIC},
            {"role": "user", "content":
                f"Team: {team_name}\nUpcoming match date: {as_of_date}\n\n"
                f"SOURCES:\n{context}\n\nReturn the JSON object."},
        ],
    }
    content = None
    for attempt in range(3):
        try:
            resp = requests.post(url, headers=headers, json=body, timeout=timeout)
        except requests.RequestException as e:
            log.warning("intel LLM call failed for %s: %s", team_name, e)
            return None
        _note_headers(resp)
        if resp.status_code == 429:  # rate limit. Short wait = per-minute window;
            wait = float(resp.headers.get("retry-after", 2 ** attempt))
            if wait > 30.0:  # long reset => per-DAY bucket: don't grind, defer.
                log.warning("intel: provider says retry in %.0fs (daily limit) on "
                            "%s -- marking budget exhausted, deferring rest.",
                            wait, team_name)
                _state["exhausted"] = True
                return None
            log.info("intel rate-limited on %s; waiting %.1fs", team_name, wait)
            time.sleep(wait)
            continue
        try:
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
        except (requests.RequestException, KeyError, ValueError) as e:
            log.warning("intel LLM call failed for %s: %s", team_name, e)
            return None
        break
    if content is None:
        log.warning("intel: still rate-limited for %s after retries -- deferring rest.",
                    team_name)
        _state["exhausted"] = True
        return None

    parsed = _extract_json(content)
    if parsed is None:
        log.warning("intel LLM returned unparseable output for %s.", team_name)
    return parsed
