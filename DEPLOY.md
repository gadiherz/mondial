# Mondial 2026 — Security & Deployment Guide

The site is a **static site** (just HTML/CSS/JS + a JSON data file). It deploys to
**Cloudflare Pages** (free, HTTPS, global CDN). The Python engine runs separately in
**GitHub Actions** on a schedule, commits the refreshed data, and the push
auto-redeploys the site. There is **no server to attack** — the public surface is
static files behind Cloudflare.

> Notation: replace `<you>` with your GitHub username and `mondial` with your repo name.

---

## 0. What's already done (in the repo)

- `web/_headers` — strict security headers for Cloudflare Pages (CSP locked to
  `'self'`, HSTS, `nosniff`, `frame-ancestors 'none'`, Permissions-Policy, etc.).
- `web/robots.txt` — allows indexing.
- The two GitHub Actions routines commit `web/data/dashboard.json` so the site
  updates after every run.
- `.env` is gitignored; **no secrets are in the repo**.

---

## 1. Move the project OUT of Google Drive

Git and Drive's sync fight each other. Copy the project to a normal folder first:

```powershell
Copy-Item "G:\My Drive\Personal\MondialPredictionModel\repo_skeleton" "C:\dev\mondial" -Recurse
cd C:\dev\mondial
```

## 2. Initialise git and verify no secrets are staged

```powershell
git init
git add .
git status          # <-- CONFIRM ".env" is NOT listed (it must be ignored)
git commit -m "Mondial 2026: initial commit"
git branch -M main
```

If `.env` appears in `git status`, STOP and check `.gitignore` before committing.

## 3. Create a PRIVATE GitHub repo and push

1. github.com → New repository → name `mondial` → **Private** → Create (don't add a
   README/.gitignore, the repo already has them).
2. ```powershell
   git remote add origin https://github.com/<you>/mondial.git
   git push -u origin main
   ```

## 4. Rotate the 5 API keys and store them as GitHub Secrets

The old keys passed through chat — **regenerate all five**, then add the NEW values
as repository secrets (never commit them). For each provider: log in → revoke/regenerate.

| Secret name (exact) | Provider to rotate at |
|---|---|
| `ODDS_API_KEY` | the-odds-api.com (account dashboard) |
| `INTEL_API_KEY` | console.groq.com (API keys) |
| `GUARDIAN_API_KEY` | open-platform.theguardian.com |
| `WORLDNEWS_API_KEY` | worldnewsapi.com (dashboard) |
| `APIFOOTBALL_API_KEY` | dashboard.api-football.com |

Add them in GitHub: **repo → Settings → Secrets and variables → Actions → New
repository secret** — one per row, names exactly as above (the workflows reference
these). Then update your local `.env` with the new values too (for local runs).

## 5. Connect Cloudflare Pages

1. dash.cloudflare.com → **Workers & Pages → Create → Pages → Connect to Git**.
2. Pick the `mondial` repo (authorise Cloudflare's GitHub app for it).
3. Build settings:
   - **Framework preset:** None
   - **Build command:** *(leave empty)*
   - **Build output directory:** `web`
4. **Save and Deploy.** Cloudflare picks up `web/_headers` and `web/robots.txt`
   automatically. Your site goes live at `https://mondial.pages.dev` (rename the
   project for a nicer subdomain, or add a custom domain later).

Every future push (including the scheduled Actions commits) redeploys automatically.

## 5b. The cron Worker (reliable clock + Winner-odds fetcher)

A small standalone **Cloudflare Worker** (`cron-worker/`) pokes the `results` job when
a match finishes AND fetches the Winner odds page (bankerim.co.il blocks GitHub's
datacenter IPs, so the fetch must happen from Cloudflare, not CI — see HANDOFF). Full
setup is in **`cron-worker/README.md`**. Two things to get right:

- The worker's `GH_TOKEN` fine-grained PAT needs **Actions: Read and write** AND
  **Contents: Read AND Write** — the write scope is what lets it commit the bankerim
  cache to `data/winner_cache/`. (A Contents: Read-only token silently fails the
  cache commit and the winner-odds job will go red on staleness.)
- When `cron-worker/worker.js` changes, **re-paste it** into the Worker editor and
  Deploy (Cloudflare does not auto-pull this standalone worker from Git).

If Cloudflare's egress is ever blocked by bankerim too, route the fetch through an
Israeli residential proxy (one secret) — the CI/parse/gate side is unchanged.

## 6. Turn on visitor analytics (no code to build)

Cloudflare gives you this for free, behind your Cloudflare login (which has 2FA +
brute-force protection — so it satisfies the "private, protected dashboard" ask).

- **Option A (keeps the strictest CSP, zero JS):** Pages project → **Metrics** shows
  requests/bandwidth/status codes. Less detail, but nothing added to the page.
- **Option B (richer: page views, countries, referrers, devices):** enable
  **Web Analytics** for the project. It injects a tiny privacy-friendly beacon, so
  **update the CSP** in `web/_headers` per the commented note there:
  - `script-src 'self' https://static.cloudflareinsights.com`
  - add `connect-src 'self' https://cloudflareinsights.com`
  Commit + push; view stats under **Analytics & Logs → Web Analytics**.

Recommended: **Option B** — it's exactly the visitor stats you wanted, privacy-first,
no cookies, and only adds one trusted Cloudflare domain to the policy.

## 7. Verify it's locked down

- Open the live site; check all three pages, the countdowns, flags, chart.
- Headers: run the URL through **securityheaders.com** — aim for **A/A+** (CSP,
  HSTS, nosniff, Referrer-Policy, Permissions-Policy all present).
- In browser DevTools → Network → click a page → Response Headers: confirm the CSP
  is the strict one and there are **no CSP violation errors** in the Console.
- Confirm the GitHub repo is **Private** and `.env` is **not** in it.

## 8. Optional polish

- **Custom domain:** Pages project → Custom domains → add yours (Cloudflare manages
  the cert).
- **HSTS preload:** once you're confident, add `; preload` to the HSTS header and
  submit at hstspreload.org (one-way — only do this when settled).
- The committed `data/mondial.db` grows over time (binary). Fine for now; consider
  Git LFS or periodic history pruning if the repo gets large.

---

## Security posture (why this is airtight)

- **No server / no database exposed publicly** — only static files on a CDN.
- **Strict CSP** with no `unsafe-inline`/`unsafe-eval`; scripts, frames, objects,
  and base-uri all locked. No external fonts, libraries, or trackers.
- **All secrets** live only in GitHub Actions Secrets (encrypted, masked in logs)
  and your local `.env` (gitignored). Rotated before going public.
- **Published data is public by nature** (match predictions/odds) — no PII, no
  credentials, nothing sensitive in `dashboard.json` or the committed DB.
- **HTTPS + HSTS** enforced by Cloudflare.
