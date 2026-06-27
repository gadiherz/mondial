// Mondial results-trigger Worker (Cloudflare Cron Trigger).
//
// Why this exists: GitHub Actions' own cron is best-effort and routinely drops
// scheduled runs for hours, so finished matches showed up late. Cloudflare cron
// fires reliably, so this tiny worker becomes the clock: every few minutes it
// reads the dashboard feed and, ONLY when a match has finished but is still
// missing its score, pokes the GitHub `results` workflow (workflow_dispatch).
//
// It ALSO owns the Winner-odds FETCH (added 2026-06). Root cause of the recurring
// Winner-odds staleness: bankerim.co.il geo/bot-blocks GitHub Actions datacenter
// IPs, so the CI scrape always got 0 rows (a local Israeli IP worked) and committed
// green -- three parser-level fixes never helped because the page never reached CI.
// Cloudflare's network is NOT on that block list, so this worker fetches the
// bankerim pages and commits the raw HTML to data/winner_cache/ (one commit, via the
// Git Data API); the winner-odds workflow then parses that cache OFFLINE and
// hard-verifies it (pipelines/refresh_winner). So the network hop happens where it
// works, and a stale feed fails the job RED instead of committing green.
//
// Crucially it is "smart": GitHub runs (and the cache commit) happen only when an
// upcoming match is actually unpriced, so this costs a handful of Actions runs +
// commits per match -- not one every few minutes all day.
//
// It reads the feed from the GitHub Contents API (not the Cloudflare site)
// because a Worker can't reliably fetch its own account's *.workers.dev URL.
//
// Secrets/vars to set in the Cloudflare dashboard (Settings -> Variables):
//   GH_TOKEN    (secret)   GitHub fine-grained PAT, repo gadiherz/mondial, with
//                          "Actions: Read and write" AND "Contents: Read and WRITE"
//                          (write is new: the worker now commits the bankerim cache).
//   TRIGGER_KEY (secret)   any random string; lets you test via the URL (?key=...).

const REPO = "gadiherz/mondial";
const FEED_API_URL =
  `https://api.github.com/repos/${REPO}/contents/web/data/dashboard.json?ref=main`;
const GIT_API = `https://api.github.com/repos/${REPO}/git`;
const DISPATCH_URL =
  `https://api.github.com/repos/${REPO}/actions/workflows/results.yml/dispatches`;
const DISPATCH_WINNER_URL =
  `https://api.github.com/repos/${REPO}/actions/workflows/winner-odds.yml/dispatches`;

const FINISH_AFTER_MIN = 110;       // a match is "done" ~110 min after kickoff
const GIVE_UP_AFTER_MIN = 8 * 60;   // stop poking 8 h after kickoff (mirrors results.py)
const WINNER_WINDOW_DAYS = 2;       // only chase Winner odds for matches within ~2 days

// bankerim aggregator pages (URL-encoded Hebrew slugs) -- MUST match
// scrapers/winner_odds.py (WINNER16_URL / WINNER_LINE_URL) so CI parses what the
// worker fetched. The committed cache is parsed by winner_odds.fetch_cached().
const BANKERIM = {
  coupon: "https://www.bankerim.co.il/%D7%9E%D7%A9%D7%97%D7%A7%D7%99%D7%9D/" +
          "%D7%95%D7%95%D7%99%D7%A0%D7%A8-16.html",
  line: "https://www.bankerim.co.il/%D7%9E%D7%A9%D7%97%D7%A7%D7%99%D7%9D/" +
        "%D7%95%D7%95%D7%99%D7%A0%D7%A8-%D7%9C%D7%99%D7%99%D7%9F-" +
        "%D7%AA%D7%95%D7%9B%D7%A0%D7%99%D7%95%D7%AA.html",
};
const CACHE_PATHS = {
  coupon: "data/winner_cache/bankerim_coupon.html",
  line: "data/winner_cache/bankerim_line.html",
};
const CACHE_META_PATH = "data/winner_cache/meta.json";
// A real bankerim page is hundreds of KB; an Imperva/geo block or challenge page is
// tiny. Below this we treat the fetch as BLOCKED and refuse to poison the cache --
// we leave the old cache to go stale so the CI gate fails RED (loud) instead of
// committing a block page that would read as "line not posted yet".
const MIN_HTML_BYTES = 20000;

function ghHeaders(env, accept) {
  if (!env.GH_TOKEN) throw new Error("GH_TOKEN secret is not set");
  return {
    "Authorization": `Bearer ${env.GH_TOKEN}`,
    "Accept": accept,
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "mondial-results-cron",   // GitHub rejects requests with no UA
  };
}

// Read the dashboard feed from GitHub. Handles both the raw media type (the file
// itself) and the default Contents response (base64-wrapped), so it works either
// way GitHub answers.
async function fetchFeed(env) {
  const r = await fetch(FEED_API_URL, { headers: ghHeaders(env, "application/vnd.github.raw") });
  if (!r.ok) throw new Error(`feed HTTP ${r.status}`);
  const data = await r.json();
  if (data && Array.isArray(data.matches)) return data;
  if (data && data.content && data.encoding === "base64") {
    const bin = atob(data.content.replace(/\s/g, ""));
    const bytes = Uint8Array.from(bin, (c) => c.charCodeAt(0));
    return JSON.parse(new TextDecoder().decode(bytes));
  }
  throw new Error("unexpected feed response shape");
}

// Return a short description of the first finished-but-unfinalised match, or null.
function matchAwaitingResult(feed, now) {
  for (const m of (feed && feed.matches) || []) {
    if (m.status === "final" || !m.kickoff_utc) continue;
    const mins = (now - Date.parse(m.kickoff_utc)) / 60000;
    if (mins >= FINISH_AFTER_MIN && mins <= GIVE_UP_AFTER_MIN) {
      return `${m.home} v ${m.away} (${Math.round(mins)} min since kickoff)`;
    }
  }
  return null;
}

// Return a description of the first upcoming match (within WINNER_WINDOW_DAYS) that
// is still missing its Winner odds, or null. The bankerim line is often posted
// AFTER Routine A's daily run; this lets the worker poke the winner-odds workflow
// so a late line is captured within the hour. Windowed to near matches because
// bankerim only lists a fixture ~1-2 days out -- chasing far-future matches would
// poke forever. Matches go null->priced once the line is scraped, ending the pokes.
function upcomingMissingWinnerOdds(feed, now) {
  for (const m of (feed && feed.matches) || []) {
    if (m.status === "final" || m.winner_odds || !m.date) continue;
    const days = (Date.parse(`${m.date}T00:00:00Z`) - now) / 86400000;
    if (days >= -1 && days <= WINNER_WINDOW_DAYS) {
      return `${m.home} v ${m.away} (${m.date}) missing Winner odds`;
    }
  }
  return null;
}

// Trigger a GitHub workflow on main by its dispatch URL. Success is HTTP 204.
async function dispatchUrl(env, url, label) {
  const r = await fetch(url, {
    method: "POST",
    headers: { ...ghHeaders(env, "application/vnd.github+json"), "Content-Type": "application/json" },
    body: JSON.stringify({ ref: "main" }),
  });
  if (r.status !== 204) {
    throw new Error(`${label} dispatch HTTP ${r.status}: ${(await r.text()).slice(0, 200)}`);
  }
}

// Trigger the GitHub `results` workflow on main. Success is HTTP 204.
async function dispatch(env) {
  await dispatchUrl(env, DISPATCH_URL, "results");
}

// --- Winner-odds cache: fetch bankerim + commit raw HTML to the repo ----------

// UTF-8 -> base64 (btoa only handles Latin-1; bankerim HTML is Hebrew/UTF-8).
function b64utf8(str) {
  const bytes = new TextEncoder().encode(str);
  let bin = "";
  for (const b of bytes) bin += String.fromCharCode(b);
  return btoa(bin);
}

// Authenticated GitHub REST JSON call (throws with a short body on non-2xx).
async function ghApi(env, method, url, body) {
  const r = await fetch(url, {
    method,
    headers: { ...ghHeaders(env, "application/vnd.github+json"),
               "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!r.ok) throw new Error(`${method} ${url} -> HTTP ${r.status}: ${(await r.text()).slice(0, 200)}`);
  return r.json();
}

// Commit several files to main in ONE commit via the Git Data API (blobs -> tree ->
// commit -> ref). Retries the whole build if main advances between read and update
// (a non-fast-forward ref PATCH), so it never force-pushes over a concurrent commit.
async function commitFiles(env, files, message) {
  for (let attempt = 1; attempt <= 3; attempt++) {
    const ref = await ghApi(env, "GET", `${GIT_API}/ref/heads/main`);
    const parent = ref.object.sha;
    const base = await ghApi(env, "GET", `${GIT_API}/commits/${parent}`);
    const tree = [];
    for (const f of files) {
      const blob = await ghApi(env, "POST", `${GIT_API}/blobs`,
                               { content: b64utf8(f.content), encoding: "base64" });
      tree.push({ path: f.path, mode: "100644", type: "blob", sha: blob.sha });
    }
    const newTree = await ghApi(env, "POST", `${GIT_API}/trees`,
                                { base_tree: base.tree.sha, tree });
    const commit = await ghApi(env, "POST", `${GIT_API}/commits`,
                               { message, tree: newTree.sha, parents: [parent] });
    try {
      await ghApi(env, "PATCH", `${GIT_API}/refs/heads/main`,
                  { sha: commit.sha, force: false });
      return commit.sha;
    } catch (e) {
      if (attempt === 3) throw e;   // out of retries -> surface the failure
      // main advanced (non-ff) -> rebuild the commit on the new HEAD and retry.
    }
  }
}

// Fetch one bankerim page with a browser-like header set (mirrors winner_odds._get).
async function fetchBankerim(url) {
  const r = await fetch(url, { headers: {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "he-IL,he;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml",
  }});
  const text = await r.text();
  return { status: r.status, text, bytes: new TextEncoder().encode(text).length };
}

// Fetch both bankerim pages and commit the healthy ones (+ a meta manifest) to the
// repo. Returns a summary. If NEITHER page is healthy (geo/bot block, or bankerim
// down) it commits NOTHING -- the old cache is left to go stale so the CI freshness
// gate fails RED, which is the loud signal we want, not a silent empty page.
async function commitWinnerCache(env, now) {
  const results = {};
  const files = [];
  for (const role of Object.keys(BANKERIM)) {
    try {
      const res = await fetchBankerim(BANKERIM[role]);
      const healthy = res.status === 200 && res.bytes >= MIN_HTML_BYTES;
      results[role] = { http_status: res.status, bytes: res.bytes, committed: healthy };
      if (healthy) files.push({ path: CACHE_PATHS[role], content: res.text });
    } catch (e) {
      results[role] = { error: String(e && e.message) };
    }
  }
  if (files.length === 0) {
    return { committed: false,
             reason: "no healthy bankerim page (blocked/down?) -> cache left to go stale (RED)",
             results };
  }
  const meta = { fetched_at: new Date(now).toISOString(), results };
  files.push({ path: CACHE_META_PATH, content: JSON.stringify(meta, null, 2) });
  const sha = await commitFiles(env, files, `winner-cache ${meta.fetched_at}`);
  return { committed: true, sha, results };
}

async function run(env, now) {
  const feed = await fetchFeed(env);
  const out = {};

  // (1) results: poke whenever a match has finished but has no score yet.
  const resultReason = matchAwaitingResult(feed, now);
  if (resultReason) {
    await dispatch(env);
    out.results = { dispatched: true, reason: resultReason };
  } else {
    out.results = { dispatched: false, reason: "no match awaiting a result" };
  }

  // (2) winner odds: when a near match is still unpriced, FETCH the bankerim pages
  // (Cloudflare reaches bankerim; CI cannot), commit the raw HTML to the repo, THEN
  // poke the winner-odds workflow -- which checks out that fresh cache and parses it
  // offline. Gated to the top-of-hour invocation so the every-10-min cron costs at
  // most ~24 fetch+commit+dispatch cycles/day (the workflow also has its own sparse
  // cron backstop). Isolated so a bankerim/commit hiccup never affects the results poke.
  if (new Date(now).getUTCMinutes() < 10) {
    const winnerReason = upcomingMissingWinnerOdds(feed, now);
    if (winnerReason) {
      try {
        const cache = await commitWinnerCache(env, now);
        await dispatchUrl(env, DISPATCH_WINNER_URL, "winner-odds");
        out.winner = { dispatched: true, reason: winnerReason, cache };
      } catch (e) {
        out.winner = { dispatched: false, reason: winnerReason,
                       error: String(e && e.message) };
      }
    } else {
      out.winner = { dispatched: false, reason: "no near match missing Winner odds" };
    }
  } else {
    out.winner = { dispatched: false, reason: "skipped (hourly gate)" };
  }
  return out;
}

export default {
  // The reliable clock: Cloudflare fires this on schedule.
  async scheduled(event, env, ctx) {
    try {
      console.log("scheduled:", JSON.stringify(await run(env, Date.now())));
    } catch (e) {
      console.error("scheduled error:", e && e.message);
    }
  },

  // Manual inspection/test endpoint (visit the worker URL in a browser):
  //   (no key)                 -> dry run: reports whether it WOULD dispatch. Never fires.
  //   ?key=TRIGGER_KEY         -> real run: dispatches iff a match is awaiting a result.
  //   ?key=TRIGGER_KEY&force=1 -> dispatch unconditionally (prove the token works).
  async fetch(request, env) {
    try {
      const url = new URL(request.url);
      const now = Date.now();
      const authed = env.TRIGGER_KEY && url.searchParams.get("key") === env.TRIGGER_KEY;
      if (authed && url.searchParams.get("force") === "1") {
        await dispatch(env);
        return Response.json({ dispatched: true, reason: "forced (manual test)" });
      }
      if (authed) {
        return Response.json(await run(env, now));
      }
      const feed = await fetchFeed(env);
      const resultReason = matchAwaitingResult(feed, now);
      const winnerReason = upcomingMissingWinnerOdds(feed, now);
      return Response.json({
        dryRun: true,
        results: { wouldDispatch: Boolean(resultReason),
                   reason: resultReason || "no match awaiting a result" },
        winner: { wouldDispatch: Boolean(winnerReason),
                  reason: winnerReason || "no near match missing Winner odds" },
      });
    } catch (e) {
      return new Response(`error: ${e && e.message}`, { status: 500 });
    }
  },
};
