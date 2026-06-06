"""Stage 1 (final): multi-provider API fetch for the team-intel corpus.

Datacenter-friendly, deploy-and-forget API sources (replaces the abandoned
bot-walled scraping):
  * Guardian Content API  -- full article bodies (primary depth, 500/day).
  * World News API        -- full article text (redundant 2nd depth, 50 pts/day).
  * API-Football          -- structured injuries (hard availability facts).

Each provider is key-optional and degrades gracefully (missing key / budget
exhausted / API error -> that provider just contributes nothing). `gather_articles`
returns full texts for the semantic distiller; `injuries_block` returns a short
structured fact line that bypasses distillation (it's already dense).
"""
from __future__ import annotations

import logging

import requests

from mondial.config import settings

log = logging.getLogger("intel.providers")
_UA = {"User-Agent": "MondialPredictionBot/1.0"}
_MIN_ARTICLE = 300


def _guardian(team: str, *, n: int = 8, timeout: float = 25.0) -> list[str]:
    if not settings.guardian_api_key:
        return []
    try:
        r = requests.get("https://content.guardianapis.com/search", params={
            "q": f"{team} World Cup", "section": "football",
            "show-fields": "bodyText", "order-by": "newest",
            "page-size": n, "api-key": settings.guardian_api_key},
            headers=_UA, timeout=timeout)
        r.raise_for_status()
        results = r.json().get("response", {}).get("results", [])
    except (requests.RequestException, ValueError) as e:
        log.warning("guardian fetch failed for %s: %s", team, e)
        return []
    out = [(a.get("fields") or {}).get("bodyText", "") for a in results]
    return [t for t in out if t and len(t) > _MIN_ARTICLE]


def _worldnews(team: str, *, n: int = 5, timeout: float = 25.0) -> list[str]:
    if not settings.worldnews_api_key:
        return []
    try:
        r = requests.get("https://api.worldnewsapi.com/search-news", params={
            "text": f"{team} football world cup", "language": "en",
            "number": n, "sort": "publish-time", "sort-direction": "DESC"},
            headers={"x-api-key": settings.worldnews_api_key, **_UA}, timeout=timeout)
        if r.status_code in (401, 402, 429):  # bad key / out of points / rate limit
            log.warning("worldnews unavailable (HTTP %d) for %s; skipping.",
                        r.status_code, team)
            return []
        r.raise_for_status()
        news = r.json().get("news", []) or []
    except (requests.RequestException, ValueError) as e:
        log.warning("worldnews fetch failed for %s: %s", team, e)
        return []
    return [a.get("text", "") for a in news if a.get("text") and len(a["text"]) > _MIN_ARTICLE]


_inj_cache: dict[str, list[str]] | None = None


def _injuries_index(*, timeout: float = 25.0) -> dict[str, list[str]]:
    """WC-wide injuries from API-Football, grouped by lowercased team name.

    Fetched once per process. Pre-tournament coverage may be empty -- that's
    fine, the prose providers also surface injuries.
    """
    global _inj_cache
    if _inj_cache is not None:
        return _inj_cache
    idx: dict[str, list[str]] = {}
    if settings.apifootball_api_key:
        try:
            r = requests.get("https://v3.football.api-sports.io/injuries",
                params={"league": 1, "season": 2026},  # league 1 = FIFA World Cup
                headers={"x-apisports-key": settings.apifootball_api_key}, timeout=timeout)
            r.raise_for_status()
            for it in r.json().get("response", []):
                tn = ((it.get("team") or {}).get("name") or "").lower()
                pl = (it.get("player") or {}).get("name") or ""
                reason = (it.get("player") or {}).get("reason") or it.get("type") or "out"
                if tn and pl:
                    idx.setdefault(tn, []).append(f"{pl} ({reason})")
        except (requests.RequestException, ValueError) as e:
            log.warning("api-football injuries failed: %s", e)
    _inj_cache = idx
    return idx


def injuries_block(team: str) -> str:
    """Short structured injury fact-line for `team` (or "")."""
    facts = _injuries_index().get(team.lower(), [])
    if not facts:
        return ""
    return "INJURY REPORT (API-Football): " + "; ".join(facts[:20])


def gather_articles(team_name: str) -> list[str]:
    """Full article texts for `team_name` from all available prose providers."""
    arts = _guardian(team_name) + _worldnews(team_name)
    log.info("providers: %s -> %d full-text articles", team_name, len(arts))
    return arts
