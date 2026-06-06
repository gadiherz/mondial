"""Routine A — Markets & Intel (dynamic, once per play-day ~2h before kickoff).

Gathers the ever-changing forward-looking signals and predicts the upcoming
slate: live odds, qualitative team intel (only for teams playing in the next
`intel_play_window_days`), then model predictions written to `predictions`,
six-player bets placed, and the static-site feed (web/data/dashboard.json)
republished.

Deliberately does NOT ingest results or re-run the rating backfill — that's
Routine B (results.py), which has the opposite cadence and immutability. This
routine consumes whatever ratings B has already produced.
"""
from __future__ import annotations

import logging
import sys

from mondial.config import settings
from mondial.data.db import init_db
from mondial.pipelines.daily_run import (
    place_virtual_bets,
    predict_upcoming,
    publish_web,
    refresh_team_intel,
    scrape_all,
    scrape_winner,
)

log = logging.getLogger("markets_intel")


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    init_db()
    scrape_all()                                              # live HDA odds (Odds API)
    scrape_winner()                                           # Winner/TOTO 1X2 (eval ref)
    refresh_team_intel(within_days=settings.intel_play_window_days)  # play-day teams
    predictions = predict_upcoming() or []                    # persist predictions
    place_virtual_bets()                                       # six-player bets @ Winner line
    publish_web()                                              # refresh static-site feed
    log.info("markets_intel complete: %d predictions, bets + web feed written",
             len(predictions))
    return 0


if __name__ == "__main__":
    sys.exit(main())
