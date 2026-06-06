"use strict";
// Mondial 2026 home page. Reads data/dashboard.json, renders countdowns + matches.
// Vanilla JS, no dependencies. All text goes in via textContent (never innerHTML
// with data), so nothing in the feed can inject markup.

const DATA_URL = "data/dashboard.json";
const POLL_MS = 60000;
const OUTCOME = { H: "home", D: "draw", A: "away" };

let STATE = { matches: [], leaderboard: [], meta: {} };
let activeTab = "upcoming";

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
  if (!tab) return;
  activeTab = tab.dataset.tab;
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t === tab));
  render();
});

load();
setInterval(tickClocks, 1000);
setInterval(load, POLL_MS);
