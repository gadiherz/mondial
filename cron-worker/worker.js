// Mondial results-trigger Worker (Cloudflare Cron Trigger).
//
// Why this exists: GitHub Actions' own cron is best-effort and routinely drops
// scheduled runs for hours, so finished matches showed up late. Cloudflare cron
// fires reliably, so this tiny worker becomes the clock: every few minutes it
// reads the live dashboard feed and, ONLY when a match has finished but is still
// missing its score, pokes the GitHub `results` workflow (workflow_dispatch).
//
// Crucially it is "smart": because GitHub runs only when a result is actually
// pending, this costs a handful of Actions runs per match -- not one every few
// minutes all day, which would blow the repo's free Actions-minute budget.
//
// Secrets/vars to set in the Cloudflare dashboard (Settings -> Variables):
//   GH_TOKEN    (secret)   GitHub fine-grained PAT, repo gadiherz/mondial,
//                          permission "Actions: Read and write".
//   TRIGGER_KEY (secret)   any random string; lets you test via the URL
//                          (?key=...). Optional but recommended.

const FEED_URL =
  "https://mondial.gadi-herz.workers.dev/data/dashboard.json";
const DISPATCH_URL =
  "https://api.github.com/repos/gadiherz/mondial/actions/workflows/results.yml/dispatches";

const FINISH_AFTER_MIN = 110;       // a match is "done" ~110 min after kickoff
const GIVE_UP_AFTER_MIN = 8 * 60;   // stop poking 8 h after kickoff (mirrors results.py)

// Read the live feed and return a short description of the first finished-but-
// unfinalised match, or null when nothing is awaiting a result.
async function matchAwaitingResult(now) {
  const res = await fetch(`${FEED_URL}?t=${now}`, { cache: "no-store" });
  if (!res.ok) throw new Error(`feed HTTP ${res.status}`);
  const data = await res.json();
  for (const m of data.matches || []) {
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
  if (!env.GH_TOKEN) throw new Error("GH_TOKEN secret is not set");
  const r = await fetch(DISPATCH_URL, {
    method: "POST",
    headers: {
      "Authorization": `Bearer ${env.GH_TOKEN}`,
      "Accept": "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": "mondial-results-cron",   // GitHub rejects requests with no UA
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ ref: "main" }),
  });
  if (r.status !== 204) {
    throw new Error(`dispatch HTTP ${r.status}: ${(await r.text()).slice(0, 200)}`);
  }
}

async function run(env, now) {
  const reason = await matchAwaitingResult(now);
  if (!reason) return { dispatched: false, reason: "no match awaiting a result" };
  await dispatch(env);
  return { dispatched: true, reason };
}

export default {
  // The reliable clock: Cloudflare fires this on schedule.
  async scheduled(event, env, ctx) {
    try {
      const out = await run(env, Date.now());
      console.log("scheduled:", JSON.stringify(out));
    } catch (e) {
      console.error("scheduled error:", e && e.message);
    }
  },

  // Manual inspection/test endpoint (visit the worker URL in a browser):
  //   (no key)            -> dry run: reports whether it WOULD dispatch. Never fires.
  //   ?key=TRIGGER_KEY    -> real run: dispatches iff a match is awaiting a result.
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
      const reason = await matchAwaitingResult(now);
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
