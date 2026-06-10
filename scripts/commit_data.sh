#!/usr/bin/env bash
# Commit + push the freshly generated data artifacts (mondial.db + dashboard.json)
# behind a HARD validation gate, so a corrupt dashboard.json can never reach the
# live site.
#
# Replaces the old, fragile flow:
#     git pull --rebase --autostash || true
#     git add ... ; git commit ; git push
# whose stash-pop conflicts wrote `<<<<<<<` markers into the deployed JSON, were
# silenced by `|| true`, and got pushed + deployed (site outage on 2026-06-10).
#
# Strategy: commit our freshly generated files first (no stash), then integrate
# remote *preferring our regenerated artifacts on conflict* (they are rebuilt in
# full each run, so newest-wins is correct), validate, and push with retry.
#
# Usage: scripts/commit_data.sh <commit-message-prefix>
set -euo pipefail

PREFIX="${1:?usage: commit_data.sh <commit-message-prefix>}"
FILES=(data/mondial.db web/data/dashboard.json)

git config user.name "mondial-bot"
git config user.email "bot@users.noreply.github.com"

# Gate #1: the file we just generated must be valid before we even stage it.
python scripts/validate_dashboard.py web/data/dashboard.json

git add "${FILES[@]}"
if git diff --cached --quiet; then
  echo "No data changes — nothing to commit."
  exit 0
fi
git commit -m "${PREFIX} $(date -u +%FT%TZ)"

# Integrate remote and push, preferring OUR freshly generated artifacts on any
# conflict. During a rebase, '-X theirs' resolves in favour of the commits being
# replayed (ours), which is exactly what we want for fully-regenerated files.
# Retry in case the remote advances between rebase and push.
for attempt in 1 2 3 4 5; do
  git fetch origin main
  if ! git rebase -X theirs origin/main; then
    echo "::error::rebase onto origin/main failed; aborting without push"
    git rebase --abort || true
    exit 1
  fi

  # Gate #2: whatever the rebase produced, never push an invalid feed.
  python scripts/validate_dashboard.py web/data/dashboard.json

  if git push; then
    echo "Pushed on attempt ${attempt}."
    exit 0
  fi
  echo "Push rejected (remote advanced); retrying (${attempt}/5)…"
  sleep $((attempt * 3))
done

echo "::error::could not push after 5 attempts"
exit 1
