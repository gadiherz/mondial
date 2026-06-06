"""Free squad/coach name list per team, for grounding the retriever to the right
people. Without this, the semantic retriever happily pulls performance analysis
about *other* teams that share the noisy corpus.

Source: the team's Wikipedia page wikitext -- national-team squad templates store
players as `|name=Player` / `|name=[[Player]]`, which is reliably regex-able.
Returns a superset (current squad + recent call-ups) -> good grounding recall.
Best-effort: an empty set just means weaker grounding, not a crash.
"""
from __future__ import annotations

import logging
import re

import requests

log = logging.getLogger("intel.roster")

_API = "https://en.wikipedia.org/w/api.php"
# Wikipedia's API blocks generic UAs; it wants a descriptive one with contact.
_UA = {"User-Agent": "MondialPredictionBot/1.0 (research; arch3dlabuh@gmail.com)"}
_NAME = r"[A-Z][A-Za-z.À-ɏ' -]{2,34}"
_cache: dict[str, set[str]] = {}


def get_ground_terms(team_name: str, *, timeout: float = 20.0) -> set[str]:
    """Lowercased grounding terms for `team_name`: player full names + surnames,
    the coach, and the team name itself."""
    if team_name in _cache:
        return _cache[team_name]

    wt = ""
    try:
        resp = requests.get(_API, params={
            "action": "parse", "page": f"{team_name} national football team",
            "prop": "wikitext", "format": "json", "redirects": 1,
            "formatversion": 2}, headers=_UA, timeout=timeout)
        resp.raise_for_status()
        wt = resp.json().get("parse", {}).get("wikitext", "") or ""
    except (requests.RequestException, ValueError) as e:
        log.warning("roster fetch failed for %s: %s", team_name, e)

    terms: set[str] = {team_name.lower()}
    for n in re.findall(rf"\|\s*name\s*=\s*\[?\[?({_NAME})", wt):
        n = n.strip().strip("[]")
        if not n:
            continue
        terms.add(n.lower())
        parts = n.split()
        if len(parts) > 1 and len(parts[-1]) >= 4:
            terms.add(parts[-1].lower())  # distinctive surname
    m = re.search(rf"\|\s*manager\s*=\s*\[?\[?({_NAME})", wt)
    if m:
        coach = m.group(1).strip()
        terms.add(coach.lower())
        cp = coach.split()
        if len(cp) > 1 and len(cp[-1]) >= 4:
            terms.add(cp[-1].lower())

    _cache[team_name] = terms
    log.info("roster: %s -> %d grounding terms", team_name, len(terms))
    return terms
