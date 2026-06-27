# mondial-results-cron — reliable results trigger + Winner-odds fetcher

GitHub Actions' cron drops scheduled runs for hours, so finished matches showed up
late. This tiny **Cloudflare Worker** is the reliable clock: every 10 minutes it
reads the live dashboard feed and, **only when a match has finished but is still
missing its score**, pokes the GitHub `results` workflow (`workflow_dispatch`).

It **also fetches the Winner odds.** The Israeli aggregator **bankerim.co.il geo/bot-
blocks GitHub Actions' datacenter IPs** (that was the root cause of the recurring
Winner-odds staleness — CI got 0 rows and committed green). Cloudflare's network is
not blocked, so when an upcoming match is still unpriced the worker fetches the
bankerim coupon+line pages, commits the raw HTML to `data/winner_cache/` (one commit,
via the Git Data API), then pokes the `winner-odds` workflow — which parses that cache
**offline** and hard-verifies it. If the worker can't get a real page (block/outage),
it commits nothing, so the cache goes stale and the winner-odds job fails **red** —
loud, never a silent empty commit.

It is separate from the site Worker, so it **cannot affect the live site**. Because
GitHub runs (and the cache commit) happen only when a result/price is actually
pending, it costs a handful of Actions runs + commits per match — not one every 10
minutes — so it stays well inside the free Actions-minute budget.

> **Updating the worker:** when `worker.js` changes (as in the 2026-06 Winner-fetch
> rework), re-paste it into the Cloudflare editor and Deploy, and make sure the token
> below has **Contents: Read AND Write** (the worker now commits the cache).

## One-time setup (Cloudflare dashboard — no Node needed)

**1. Create the GitHub token** (if you haven't): GitHub → Settings → Developer
settings → **Fine-grained tokens** → Generate. Repository access: **Only
`gadiherz/mondial`**. Permissions → Repository → **Actions: Read and write** AND
**Contents: Read and write** (the worker reads the feed AND commits the bankerim
cache via the API). Generate and copy it. *(If your existing token has Contents:
Read only, edit it and upgrade Contents to Read and write — without write the
Winner-odds cache commit fails.)*

**2. Create the Worker:** Cloudflare dashboard → Workers & Pages → **Create** →
**Create Worker** → name it `mondial-results-cron` → Deploy. Then **Edit code**,
delete the placeholder, paste the entire contents of [`worker.js`](./worker.js),
and **Deploy**.

**3. Add the secrets:** the Worker → **Settings → Variables and Secrets** → add two
**Secrets**:
- `GH_TOKEN` = the GitHub token from step 1
- `TRIGGER_KEY` = any random string you invent (used only to let you test via URL)

Deploy/save after adding them.

**4. Add the cron trigger:** the Worker → **Settings → Triggers → Cron Triggers** →
Add → `*/10 * * * *` → Add.

## Verify it works (do this once, before trusting it)

Your worker URL is `https://mondial-results-cron.<your-subdomain>.workers.dev`.
(Add the `GH_TOKEN` secret first — the worker reads the feed via the GitHub API,
so even the dry run needs it.)

1. **Logic check** — open the URL plain. You should get JSON like
   `{"dryRun":true,"wouldDispatch":false,"reason":"no match awaiting a result"}`.
   That proves it reads the feed and evaluates correctly. (`wouldDispatch` is only
   `true` in the ~2-hour window after a match ends and before its score lands.)
2. **End-to-end trigger check** — open
   `https://…workers.dev/?key=YOUR_TRIGGER_KEY&force=1`. You should get
   `{"dispatched":true,…}`, and within a minute a new **results** run appears in
   GitHub → Actions (triggered "manually" / via workflow_dispatch). That proves the
   token and the GitHub call work end-to-end.

That's it. From then on, a finished match is picked up within ~10 minutes,
automatically. The GitHub workflow keeps a 6-hour backup schedule in case the
worker is ever down.

## Optional: deploy via CLI instead (needs Node)

```
cd cron-worker
npx wrangler deploy
npx wrangler secret put GH_TOKEN
npx wrangler secret put TRIGGER_KEY
```

`wrangler.jsonc` already defines the name and the `*/10 * * * *` cron.
