"""Stage 1: free raw-information sources for team intel (no API key, no cost).

Local LLMs can't browse, so we fetch the current text ourselves and hand it to
the processor. Two free sources:
  * Google News RSS  -- recent headlines+snippets (injuries, coach, morale, form).
  * Wikipedia REST summary -- a stable baseline (who the coach is, what the team is).

Everything here is best-effort: any source that fails is skipped, and an empty
context is a valid (low-confidence) result the processor handles.
"""
from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from urllib.parse import quote

import requests

from mondial.config import settings

log = logging.getLogger("intel.sources")

_UA = {"User-Agent": "Mozilla/5.0 (compatible; MondialBot/1.0)"}
_NEWS = "https://news.google.com/rss/search"
_WIKI = "https://en.wikipedia.org/api/rest_v1/page/summary/"

_TAGS = re.compile(r"<[^>]+>")


def _strip_html(s: str) -> str:
    return re.sub(r"\s+", " ", _TAGS.sub(" ", s or "")).strip()


def news_entries(query: str, *, max_items: int = 20, timeout: float = 20.0) -> list[dict]:
    """Google News RSS items as dicts: {title, link, pub, desc}.

    `link` is the (redirect-wrapped) article URL — used by the deep scraper.
    """
    try:
        resp = requests.get(
            _NEWS, params={"q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"},
            headers=_UA, timeout=timeout)
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
    except (requests.RequestException, ET.ParseError) as e:
        log.warning("news fetch failed for %r: %s", query, e)
        return []
    out = []
    for item in root.iter("item"):
        out.append({
            "title": _strip_html(item.findtext("title") or ""),
            "link": (item.findtext("link") or "").strip(),
            "pub": (item.findtext("pubDate") or "")[:16],
            "desc": _strip_html(item.findtext("description") or ""),
        })
        if len(out) >= max_items:
            break
    return out


def _news(query: str, *, max_items: int, timeout: float) -> list[str]:
    out = []
    for e in news_entries(query, max_items=max_items, timeout=timeout):
        line = f"[{e['pub']}] {e['title']}"
        if e["desc"] and e["desc"] not in e["title"]:
            line += f" -- {e['desc']}"
        if line.strip():
            out.append(line)
    return out


def _wikipedia(team_name: str, *, timeout: float) -> str:
    try:
        resp = requests.get(
            _WIKI + quote(f"{team_name} national football team"),
            headers=_UA, timeout=timeout)
        if resp.status_code != 200:
            return ""
        return _strip_html(resp.json().get("extract", ""))
    except (requests.RequestException, ValueError) as e:
        log.warning("wikipedia fetch failed for %s: %s", team_name, e)
        return ""


def gather_team_context(
    team_name: str, *, max_news: int = 12, timeout: float = 20.0,
    max_chars: int | None = None,
) -> str:
    """Assemble a free current-info blob for `team_name` (may be empty).

    The result is hard-capped at `max_chars` (default `settings.
    intel_max_context_chars`) so the downstream LLM call has a bounded,
    predictable token cost -- this is the source-side half of the data-flow
    budget guard (the run-level stop is in collect.py).
    """
    cap = settings.intel_max_context_chars if max_chars is None else max_chars
    headlines: list[str] = []
    seen: set[str] = set()
    for q in (f'"{team_name}" national football team injury squad coach',
              f'"{team_name}" World Cup 2026 squad'):
        for line in _news(q, max_items=max_news, timeout=timeout):
            key = line.split("] ", 1)[-1][:80]
            if key not in seen:
                seen.add(key)
                headlines.append(line)
        if len(headlines) >= max_news:
            break

    parts = []
    wiki = _wikipedia(team_name, timeout=timeout)
    if wiki:
        parts.append("BACKGROUND (Wikipedia):\n" + wiki[:800])
    if headlines:
        parts.append("RECENT NEWS:\n" + "\n".join(headlines[:max_news]))
    blob = "\n\n".join(parts)
    return blob[:cap]
