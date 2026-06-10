#!/usr/bin/env python3
"""Hard validation gate for the deployed dashboard feed.

Exits non-zero (failing the CI job) if web/data/dashboard.json is not strictly
valid JSON, still contains git conflict markers, or is missing the structure the
static site needs. The scheduled workflows run this AFTER integrating remote
changes and BEFORE `git push`, so a broken feed can never reach the live site.

Background: on 2026-06-10 a `git pull --rebase --autostash` stash-pop conflict
wrote `<<<<<<< Updated upstream` markers straight into dashboard.json; `|| true`
swallowed the failure and the corrupt file was committed, pushed and deployed,
blanking the site. This gate makes that class of failure impossible to deploy.
"""
import json
import sys

# Conflict markers only ever appear at the start of a line.
CONFLICT_MARKERS = ("<<<<<<<", "=======", ">>>>>>>")


def fail(msg: str) -> None:
    print(f"::error::dashboard validation failed: {msg}")
    sys.exit(1)


def main() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else "web/data/dashboard.json"
    try:
        raw = open(path, encoding="utf-8").read()
    except OSError as e:
        fail(f"cannot read {path}: {e}")

    for line_no, line in enumerate(raw.splitlines(), 1):
        for marker in CONFLICT_MARKERS:
            if line.startswith(marker):
                fail(f"unresolved git conflict marker {marker!r} at line {line_no} of {path}")

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        fail(f"invalid JSON in {path}: {e}")

    if not isinstance(data, dict):
        fail("top-level value is not a JSON object")
    meta = data.get("meta")
    if not isinstance(meta, dict) or "generated_at" not in meta:
        fail("missing meta.generated_at")
    if not isinstance(data.get("matches"), list):
        fail("missing matches[] array")

    print(f"dashboard OK: {len(data['matches'])} matches, "
          f"generated_at={meta['generated_at']}")


if __name__ == "__main__":
    main()
