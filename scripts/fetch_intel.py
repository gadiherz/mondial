"""Autonomously research every upcoming-fixture team and store its intel vector.

Runs the free RAG intel pipeline (mondial.intel): full-text news APIs ->
roster grounding -> local semantic distillation -> a hosted LLM scoring call per
team. Needs INTEL_API_KEY in .env (a free Groq key from console.groq.com by
default; or point intel_base_url at a local Ollama and leave it blank). The
corpus providers optionally use GUARDIAN_API_KEY / WORLDNEWS_API_KEY /
APIFOOTBALL_API_KEY -- each degrades gracefully if absent. Idempotent.

Usage:
    PYTHONPATH=src python scripts/fetch_intel.py            # all upcoming teams
    PYTHONPATH=src python scripts/fetch_intel.py --limit 5  # first 5 (budget probe)
"""
import argparse
import logging

from mondial.data.db import connect, init_db
from mondial.intel.collect import collect_intel


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None,
                        help="cap number of teams this run (API budget)")
    parser.add_argument("--model", default=None, help="override intel model")
    args = parser.parse_args()

    init_db()
    with connect() as conn:
        n = collect_intel(conn, model=args.model, limit=args.limit)
        total = conn.execute("SELECT COUNT(*) AS n FROM team_intel").fetchone()["n"]
    logging.getLogger("fetch_intel").info(
        "team_intel now holds %d rows (%d written this run)", total, n)


if __name__ == "__main__":
    main()
