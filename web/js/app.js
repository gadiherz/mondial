"use strict";
// Mondial 2026 home page. Reads data/dashboard.json, renders countdowns + matches.
// Vanilla JS, no dependencies. All text goes in via textContent (never innerHTML
// with data), so nothing in the feed can inject markup.

const DATA_URL = "data/dashboard.json";
const POLL_MS = 60000;
const OUTCOME = { H: "home", D: "draw", A: "away" };

let STATE = { matches: [], leaderboard: [], meta: {} };
let activeTab = "upcoming";
// Which match panels are expanded, keyed stably so the open state survives the
// 60s re-render (the feed carries no match_id, so key on date + teams).
const openMatches = new Set();
const matchId = (m) => `${m.date}|${m.home}|${m.away}`;

const $ = (sel, root = document) => root.querySelector(sel);
function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
}

// ---- data ----------------------------------------------------------------
async function load() {
  try {
    const res = await fetch(`${DATA_URL}?t=${Date.now()}`, { cache: "no-store" });
    if (!res.ok) throw new Error(res.status);
    STATE = await res.json();
    render();
  } catch (e) {
    if (!STATE.matches.length) $("#matches").replaceChildren(
      el("p", "empty", "Couldn’t load match data. It’ll appear once the model has run."));
  }
}

// ---- countdowns ----------------------------------------------------------
function fmt(ms) {
  if (ms <= 0) return "live now";
  const s = Math.floor(ms / 1000);
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60), sec = s % 60;
  const p = (n) => String(n).padStart(2, "0");
  return d > 0 ? `${d}d ${p(h)}:${p(m)}:${p(sec)}` : `${p(h)}:${p(m)}:${p(sec)}`;
}
function tickClocks() {
  const now = Date.now();
  const targets = { kickoff: STATE.meta.kickoff_utc, scrape: STATE.meta.next_scrape_utc };
  document.querySelectorAll("[data-clock]").forEach((node) => {
    const t = targets[node.dataset.clock];
    node.textContent = t ? fmt(new Date(t).getTime() - now) : "—";
  });
  // Deploy-and-forget: once kickoff passes, retire its countdown automatically and
  // centre the remaining "next model update" card.
  const ko = STATE.meta.kickoff_utc ? new Date(STATE.meta.kickoff_utc).getTime() : null;
  const passed = ko !== null && ko - now <= 0;
  const kc = document.getElementById("cd-kickoff");
  const wrap = document.getElementById("countdowns");
  if (kc) kc.style.display = passed ? "none" : "";
  if (wrap) wrap.classList.toggle("single", passed);
}

// ---- matches -------------------------------------------------------------
function dateLabel(iso) {
  const d = new Date(iso + "T12:00:00Z");
  return d.toLocaleDateString(undefined, { weekday: "long", month: "long", day: "numeric" });
}
function flagImg(code, name) {
  if (!code) return null;
  const img = el("img", "flag");
  img.src = `assets/flags/${code}.svg`;
  img.alt = ""; img.width = 26; img.height = 18; img.loading = "lazy";
  return img;
}
function teamSide(name, code, cls) {
  const wrap = el("div", `team ${cls}`);
  const flag = flagImg(code, name);
  const nameEl = el("span", "team-name", name);
  if (cls === "away") {            // name then flag (row is right-aligned)
    wrap.appendChild(nameEl);
    if (flag) wrap.appendChild(flag);
  } else {                          // flag then name
    if (flag) wrap.appendChild(flag);
    wrap.appendChild(nameEl);
  }
  return wrap;
}
function pickLabel(m) {
  if (!m.model_pick) return null;
  return m.model_pick === "D" ? "Draw" : m[OUTCOME[m.model_pick]];
}

// Left-labelled 1/X/2 grid: each column = a box holding the Winner odd with the
// model probability one line below it. Picked outcome highlighted (when priced).
function marketGrid(m) {
  const cells = [["home", "H"], ["draw", "D"], ["away", "A"]];
  const g = el("div", "market");
  g.appendChild(el("span", "m-corner"));
  ["1", "X", "2"].forEach((k) => g.appendChild(el("span", "m-key", k)));
  g.appendChild(el("span", "m-rowlabel", "Winner odds"));
  cells.forEach(([key, code]) => {
    const pick = m.winner_odds && m.model_pick === code ? " pick" : "";
    g.appendChild(el("span", "m-odd" + pick,
      m.winner_odds ? m.winner_odds[key].toFixed(2) : "—"));
  });
  g.appendChild(el("span", "m-rowlabel", "Model probability"));
  cells.forEach(([key, code]) => {
    const pick = m.winner_odds && m.model_pick === code ? " pick" : "";
    g.appendChild(el("span", "m-prob" + pick, Math.round(m.model[key] * 100) + "%"));
  });
  return g;
}

// Fallback messaging when a match has no Winner 1X2 odds.
function marketNote(m) {
  if (m.winner_odds) return null;
  if (m.status === "final")
    return el("div", "no-odds excluded",
      "No Winner 1X2 odds were offered — this match is excluded from the players’ standings.");
  return el("div", "no-odds", "Winner odds open closer to kickoff");
}

// ---- prediction breakdown ("why this prediction") -----------------------
// Friendly labels for the model's internal feature names and intel dimensions.
const FEAT_LABEL = {
  glicko_diff: "Rating edge", rd_sum: "Uncertainty",
  resid_ewma_diff: "Momentum", gd_ewma_diff: "Goal-diff form", idle_days_diff: "Rest",
};
const DIM_LABEL = {
  form_outlook: "Form", availability: "Availability",
  coach_stability: "Coach", morale: "Morale", motivation: "Motivation",
};
const pct = (x) => Math.round(x * 100) + "%";
const signed = (x, d = 2) => (x >= 0 ? "+" : "") + Number(x).toFixed(d);

function bdSection(title) {
  const s = el("div", "bd-section");
  s.appendChild(el("div", "bd-h", title));
  return s;
}

// A centre-origin diverging bar: positive grows right (home, accent), negative
// grows left (away, blue). `max` normalises the width across sibling rows.
function divBar(value, max) {
  const track = el("div", "bd-bar");
  const fill = el("span", "bd-fill " + (value >= 0 ? "pos" : "neg"));
  const frac = max > 0 ? Math.min(Math.abs(value) / max, 1) : 0;
  fill.style.width = (frac * 50).toFixed(1) + "%";
  track.appendChild(fill);
  return track;
}

// One labelled diverging row: [label] [bar] [value].
function bdBarRow(label, value, max, valText, title) {
  const row = el("div", "bd-row");
  const l = el("span", "bd-label", label);
  if (title) l.title = title;
  row.appendChild(l);
  row.appendChild(divBar(value, max));
  row.appendChild(el("span", "bd-val", valText));
  return row;
}

function bdGoals(b, m) {
  const s = bdSection("Expected goals");
  const g = el("div", "bd-goals");
  const mk = (name, lam) => {
    const c = el("div", "bd-goal");
    c.appendChild(el("div", "bd-goal-lam", lam.toFixed(2)));
    c.appendChild(el("div", "bd-goal-team", name));
    return c;
  };
  g.appendChild(mk(m.home, b.lambda.home));
  g.appendChild(el("div", "bd-goal-x", "–"));
  g.appendChild(mk(m.away, b.lambda.away));
  s.appendChild(g);
  if (b.scorelines && b.scorelines.length) {
    const chips = el("div", "bd-chips");
    chips.appendChild(el("span", "bd-chips-lab", "Likely scores"));
    b.scorelines.slice(0, 5).forEach((sc) => {
      const chip = el("span", "bd-chip");
      chip.appendChild(el("span", "bd-chip-s", `${sc.h}–${sc.a}`));
      chip.appendChild(el("span", "bd-chip-p", pct(sc.p)));
      chips.appendChild(chip);
    });
    s.appendChild(chips);
  }
  return s;
}

function bdFeatures(b) {
  const s = bdSection("What the model weighed");
  s.appendChild(el("p", "bd-note", "Bars lean right toward the home side, left toward the away side."));
  const max = Math.max(...b.features.map((f) => Math.abs(f.contrib_home)), 1e-6);
  b.features.forEach((f) => {
    const label = FEAT_LABEL[f.name] || f.name;
    s.appendChild(bdBarRow(label, f.contrib_home, max, signed(f.contrib_home, 2), f.description));
  });
  return s;
}

function bdRatings(b, m) {
  const s = bdSection("Ratings · Glicko-2");
  const grid = el("div", "bd-kv");
  const line = (team, g) => {
    grid.appendChild(el("span", "bd-k", team));
    let v = `${g.rating} ± ${g.rd}`;
    if (g.intel_points) v += `  (${signed(g.intel_points, 0)} scouting)`;
    grid.appendChild(el("span", "bd-v", v));
  };
  line(m.home, b.glicko.home);
  line(m.away, b.glicko.away);
  s.appendChild(grid);
  const exp = b.glicko.home_expected;
  s.appendChild(bdBarRow("Win expectancy", exp - 0.5, 0.5,
    `${m.home} ${pct(exp)}`, "Pure rating-based win expectancy (pre-intel)."));
  return s;
}

function bdMomentum(b, m) {
  const s = bdSection("Momentum · form");
  const grid = el("div", "bd-kv");
  const line = (team, mo) => {
    grid.appendChild(el("span", "bd-k", team));
    grid.appendChild(el("span", "bd-v",
      `vs expectation ${signed(mo.resid_ewma, 2)} · goal-diff form ${signed(mo.gd_ewma, 2)} · ${mo.idle_days}d rest`));
  };
  line(m.home, b.momentum.home);
  line(m.away, b.momentum.away);
  s.appendChild(grid);
  return s;
}

function bdIntelSide(team, intel) {
  const box = el("div", "bd-intel-side");
  const head = el("div", "bd-intel-head");
  head.appendChild(el("span", "bd-intel-team", team));
  const badge = el("span", "bd-badge " + intel.status, intel.status.replace("_", " "));
  head.appendChild(badge);
  box.appendChild(head);
  if (intel.summary) box.appendChild(el("p", "bd-intel-sum", intel.summary));
  if (intel.dims) {
    const dims = el("div", "bd-dims");
    Object.entries(intel.dims).forEach(([k, val]) => {
      if (val == null) return;
      dims.appendChild(bdBarRow(DIM_LABEL[k] || k, val, 1, signed(val, 2)));
    });
    box.appendChild(dims);
  }
  if (intel.status !== "informed" && !intel.summary)
    box.appendChild(el("p", "bd-note", "No qualitative signal — ratings only."));
  return box;
}

function bdIntel(b, m) {
  const s = bdSection("AI scout report");
  const wrap = el("div", "bd-intel");
  wrap.appendChild(bdIntelSide(m.home, b.intel.home));
  wrap.appendChild(bdIntelSide(m.away, b.intel.away));
  s.appendChild(wrap);
  return s;
}

function bdFinal(b) {
  const s = bdSection("Final probabilities");
  const c = b.calibration;
  const names = ["Home", "Draw", "Away"];
  const grid = el("div", "bd-final");
  names.forEach((n, i) => {
    const col = el("div", "bd-final-col");
    col.appendChild(el("span", "bd-final-k", n));
    col.appendChild(el("span", "bd-final-raw", pct(c.raw[i])));
    col.appendChild(el("span", "bd-final-arrow", "→"));
    col.appendChild(el("span", "bd-final-cal", pct(c.calibrated[i])));
    grid.appendChild(col);
  });
  s.appendChild(grid);
  if (c.temperature != null)
    s.appendChild(el("p", "bd-note",
      `Raw model → calibrated (temperature T = ${c.temperature.toFixed(2)}).`));
  return s;
}

function breakdownPanel(m) {
  const b = m.breakdown;
  const panel = el("div", "breakdown");
  panel.appendChild(bdGoals(b, m));
  panel.appendChild(bdFeatures(b));
  panel.appendChild(bdRatings(b, m));
  panel.appendChild(bdMomentum(b, m));
  panel.appendChild(bdIntel(b, m));
  panel.appendChild(bdFinal(b));
  return panel;
}

function matchCard(m) {
  const card = el("article", "match");

  const top = el("div", "match-top");
  const meta = el("div", "match-meta");
  meta.appendChild(el("span", "stage-pill", (m.stage || "group").toUpperCase()));
  meta.appendChild(el("span", null, dateLabel(m.date)));
  if (m.venue) meta.appendChild(el("span", null, "· " + m.venue));
  top.appendChild(meta);
  if (m.status === "final" && m.score)
    top.appendChild(el("span", "score", `${m.score[0]}–${m.score[1]}`));
  card.appendChild(top);

  const teams = el("div", "teams");
  teams.appendChild(teamSide(m.home, m.home_code, "home"));
  teams.appendChild(el("span", "vs", "vs"));
  teams.appendChild(teamSide(m.away, m.away_code, "away"));
  card.appendChild(teams);

  // Market grid (Winner odds + model probability) and any no-odds fallback note.
  if (m.model) {
    card.appendChild(marketGrid(m));
    const note = marketNote(m);
    if (note) card.appendChild(note);
  }

  const label = pickLabel(m);
  if (label) {
    const line = el("div", "pick-line");
    line.appendChild(el("span", "pick-badge", "MODEL BACKS"));
    line.appendChild(el("span", null, label));
    if (m.status === "final" && m.result) {
      const ok = m.result === m.model_pick;
      line.appendChild(el("span", ok ? "pick-correct" : "pick-wrong", ok ? "✓ hit" : "✗ miss"));
    }
    card.appendChild(line);
  }

  // Expandable "why this prediction" panel. Present only when the feed carries a
  // breakdown for this match; clicking the card toggles it (state survives polls).
  if (m.breakdown) {
    const mid = matchId(m);
    card.classList.add("has-why");
    card.dataset.mid = mid;
    const toggle = el("div", "bd-toggle");
    toggle.appendChild(el("span", "bd-toggle-t", "Why this prediction"));
    toggle.appendChild(el("span", "bd-caret", "▾"));
    card.appendChild(toggle);
    card.appendChild(breakdownPanel(m));
    if (openMatches.has(mid)) card.classList.add("open");
  }
  return card;
}

function render() {
  tickClocks();
  if (STATE.meta.generated_at) {
    $("#updated").textContent = "Updated " +
      new Date(STATE.meta.generated_at).toLocaleString();
  }

  const want = activeTab === "results" ? "final" : null;
  const list = STATE.matches.filter((m) =>
    want === "final" ? m.status === "final" : m.status !== "final");
  const host = $("#matches");

  if (!list.length) {
    host.replaceChildren(el("p", "empty", activeTab === "results"
      ? "No results yet — the tournament starts June 11." : "No upcoming matches."));
    return;
  }

  const groups = new Map();
  for (const m of list) (groups.get(m.date) || groups.set(m.date, []).get(m.date)).push(m);
  const frag = document.createDocumentFragment();
  for (const [date, ms] of groups) {
    const g = el("section", "date-group");
    g.appendChild(el("div", "date-head", dateLabel(date)));
    const wrap = el("div", "matches");
    ms.forEach((m) => wrap.appendChild(matchCard(m)));
    g.appendChild(wrap);
    frag.appendChild(g);
  }
  host.replaceChildren(frag);
}

// ---- wire-up -------------------------------------------------------------
document.addEventListener("click", (e) => {
  const tab = e.target.closest(".tab");
  if (tab) {
    activeTab = tab.dataset.tab;
    document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t === tab));
    render();
    return;
  }
  // Toggle a match's "why this prediction" panel (only cards that have one).
  const card = e.target.closest(".match.has-why");
  if (card) {
    const mid = card.dataset.mid;
    const open = card.classList.toggle("open");
    if (open) openMatches.add(mid); else openMatches.delete(mid);
  }
});

load();
setInterval(tickClocks, 1000);
setInterval(load, POLL_MS);

// A tab left open/backgrounded gets its poll timer throttled or frozen by the
// browser, and load() silently keeps the last STATE if a refresh fetch fails --
// both can leave a stale snapshot (e.g. Results missing the newest finals). So
// force a fresh fetch + re-render the moment the tab is shown or focused again;
// it can never sit on old data.
function refreshIfVisible() { if (!document.hidden) load(); }
document.addEventListener("visibilitychange", refreshIfVisible);
window.addEventListener("focus", refreshIfVisible);
window.addEventListener("pageshow", refreshIfVisible);
