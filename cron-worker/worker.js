// Mondial results-trigger Worker (Cloudflare Cron Trigger).
//
// Why this exists: GitHub Actions' own cron is best-effort and routinely drops
// scheduled runs for hours, so finished matches showed up late. Cloudflare cron
// fires reliably, so this tiny worker becomes the clock: every few minutes it
// reads the dashboard feed and, ONLY when a match has finished but is still
// missing its score, pokes the GitHub `results` workflow (workflow_dispatch).
//
// Crucially it is "smart": because GitHub runs only when a result is actually
// pending, this costs a handful of Actions runs per match -- not one every few
// minutes all day, which would blow the repo's free Actions-minute budget.
//
// It reads the feed from the GitHub Contents API (not the Cloudflare site)
// because a Worker can't reliably fetch its own account's *.workers.dev URL.
//
// Secrets/vars to set in the Cloudflare dashboard (Settings -> Variables):
//   GH_TOKEN    (secret)   GitHub fine-grained PAT, repo gadiherz/mondial, with
//                          "Actions: Read and write" AND "Contents: Read".
//   TRIGGER_KEY (secret)   any random string; lets you test via the URL (?key=...).

const REPO = "gadiherz/mondial";
const FEED_API_URL =
  `https://api.github.com/repos/${REPO}/contents/web/data/dashboard.json?ref=main`;
const DISPATCH_URL =
  `https://api.github.com/repos/${REPO}/actions/workflows/results.yml/dispatches`;

const FINISH_AFTER_MIN = 110;       // a match is "done" ~110 min after kickoff
const GIVE_UP_AFTER_MIN = 8 * 60;   // stop poking 8 h after kickoff (mirrors results.py)

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

// Trigger the GitHub `results` workflow on main. Success is HTTP 204.
async function dispatch(env) {
  const r = await fetch(DISPATCH_URL, {
    method: "POST",
    headers: { ...ghHeaders(env, "application/vnd.github+json"), "Content-Type": "application/json" },
    body: JSON.stringify({ ref: "main" }),
  });
  if (r.status !== 204) {
    throw new Error(`dispatch HTTP ${r.status}: ${(await r.text()).slice(0, 200)}`);
  }
}

async function run(env, now) {
  const reason = matchAwaitingResult(await fetchFeed(env), now);
  if (!reason) return { dispatched: false, reason: "no match awaiting a result" };
  await dispatch(env);
  return { dispatched: true, reason };
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
      const reason = matchAwaitingResult(await fetchFeed(env), now);
      return Response.json({
        dryRun: true,
        wouldDispatch: Boolean(reason),
        reason: reason || "no match awaiting a result",
      });
    } catch (e) {
      return new Response(`error: ${e && e.message}`, { status: 500 });
    }
  },
};
