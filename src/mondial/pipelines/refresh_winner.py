"""Routine C — Winner-odds refresh (cheap, frequent, self-healing).

Why this exists separate from Routine A (markets_intel): the bankerim Winner line
for a play-day is often posted AFTER Routine A's once-a-day 14:00 UTC run, so that
run scrapes 0 national-team fixtures and the matches stay unpriced all day. This
routine re-attempts ONLY the Winner scrape (no Odds API, no LLM intel, no
re-predict), so it is cheap enough to run several times a day and to be poked by
the Cloudflare cron worker whenever upcoming matches still lack a Winner price.

Steps (all idempotent):
  scrape_winner()      -> upsert bookmaker='winner' rows for any newly-posted line
  place_virtual_bets() -> place the six-player bets for matches now priced (catch-up
                          for fixtures Routine A saw before their odds were posted)
  publish_web()        -> republish web/data/dashboard.json (clears the staleness
                          canary the frontend shows once odds arrive)

Predictions are NOT regenerated here -- Routine A already wrote them; this only
adds the late Winner line and the bets/feed that depend on it.

Run: python -m mondial.pipelines.refresh_winner
"""
from __future__ import annotations

import logging
import sys

from mondial.data.db import init_db
from mondial.pipelines.daily_run import (
    place_virtual_bets,
    publish_web,
    scrape_winner,
)

log = logging.getLogger("refresh_winner")


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    init_db()
    scrape_winner()        # Winner/TOTO 1X2 (eval ref) -- the only network call
    place_virtual_bets()   # six-player bets for matches now priced (idempotent)
    publish_web()          # refresh static-site feed + clear staleness canary
    log.info("refresh_winner complete: Winner odds re-attempted, bets + feed written")
    return 0


if __name__ == "__main__":
    sys.exit(main())
