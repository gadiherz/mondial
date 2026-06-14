"use strict";
// Players leaderboard. Reads data/dashboard.json (leaderboard[]). Vanilla JS;
// all feed data inserted via textContent.

const DATA_URL = "data/dashboard.json";
const POLL_MS = 60000;

// Character + personality metadata (static, developer-authored).
const META = {
  model_full:        { svg: "quant",  name: "The Quant",     color: "#28e0a8", desc: "Backs the best value vs the Winner odds." },
  model_fundamental: { svg: "purist", name: "The Purist",    color: "#4f8cff", desc: "Pure model — ignores the market entirely." },
  safe:              { svg: "banker", name: "The Banker",    color: "#f6c659", desc: "Always backs the favourite — the lowest odd." },
  tide:              { svg: "draw",   name: "Mr. Draw",      color: "#8a97b8", desc: "Bets the draw. Every. Single. Time." },
  risk:              { svg: "risk",   name: "The Daredevil", color: "#ff6b6b", desc: "Always the longest odds. Go big or go home." },
  monkey:            { svg: "monkey", name: "The Monkey",    color: "#c084fc", desc: "Picks at random — the control group." },
};
const ORDER = Object.keys(META);
const SVGNS = "http://www.w3.org/2000/svg";
// Which player cards are expanded to show their bet history (survives the 60s
// re-render, keyed by player name).
const openPlayers = new Set();
const OUTCOME_LABEL = { H: "home", D: "draw", A: "away" };

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
}
// Keep the half-shekels: show up to 2 decimals, trimming trailing zeros
// (₪39.5, ₪14, ₪28.75) so payouts aren't rounded into the wrong number.
const money = (n) => {
  const s = Math.abs(n).toFixed(2).replace(/\.?0+$/, "");
  return (n < 0 ? "−₪" : "₪") + s;
};
const signedMoney = (n) => (n >= 0 ? "+" : "") + money(n);   // +₪4 / −₪10

// ---- bet history (per-player drill-down) ---------------------------------
const pickName = (b) => (b.pick === "D" ? "Draw" : b.pick === "H" ? b.home : b.away);
const shortDate = (iso) =>
  new Date(iso + "T12:00:00Z").toLocaleDateString(undefined, { month: "short", day: "numeric" });

function betRow(b) {
  const settledCls = b.settled ? (b.won ? " won" : " lost") : " pending";
  const row = el("div", "bet" + settledCls);

  const left = el("div", "bet-left");
  left.appendChild(el("span", "bet-date", shortDate(b.date)));
  const desc = el("div", "bet-desc");
  desc.appendChild(el("span", "bet-pick", pickName(b)));
  desc.appendChild(el("span", "bet-match", `${b.home} v ${b.away}`));
  left.appendChild(desc);
  row.appendChild(left);

  const right = el("div", "bet-right");
  if (b.settled) {
    if (b.score) right.appendChild(el("span", "bet-score", `${b.score[0]}–${b.score[1]}`));
    right.appendChild(el("span", "bet-tag " + (b.won ? "won" : "lost"), b.won ? "WON" : "LOST"));
    right.appendChild(el("span", "bet-money " + (b.profit >= 0 ? "pos" : "neg"), signedMoney(b.profit)));
  } else {
    right.appendChild(el("span", "bet-tag pending", "PENDING"));
    right.appendChild(el("span", "bet-stake", `${money(b.stake)} @ ${b.odd}`));
  }
  row.appendChild(right);
  return row;
}

function betsPanel(bets) {
  const panel = el("div", "bets-panel");
  if (!bets || !bets.length) {
    panel.appendChild(el("p", "bets-empty", "No bets placed yet — they appear on match day."));
    return panel;
  }
  bets.forEach((b) => panel.appendChild(betRow(b)));
  return panel;
}

function statBlock(label, value, cls) {
  const s = el("div", "stat");
  s.appendChild(el("div", "stat-v" + (cls ? " " + cls : ""), value));
  s.appendChild(el("div", "stat-k", label));
  return s;
}

function card(row, rank) {
  const meta = META[row.player] || { svg: "monkey", name: row.player, desc: "" };
  const c = el("article", "player-card");

  const head = el("div", "pc-head");
  const av = el("img", "pc-avatar");
  av.src = `assets/players/${meta.svg}.svg`;
  av.alt = meta.name;
  av.width = 64; av.height = 64; av.loading = "lazy";
  head.appendChild(av);
  const id = el("div", "pc-id");
  const nameRow = el("div", "pc-name");
  nameRow.appendChild(el("span", null, meta.name));
  nameRow.appendChild(el("span", "pc-rank", "#" + rank));
  id.appendChild(nameRow);
  id.appendChild(el("div", "pc-desc", meta.desc));
  head.appendChild(id);
  c.appendChild(head);

  const stats = el("div", "pc-stats");
  stats.appendChild(statBlock("Risked", money(row.staked)));
  // "Made" = money actually returned (gross), e.g. ₪14 on Mexico + ₪25.5 on
  // South Korea = ₪39.5 back on ₪20 risked. Coloured by whether that beats the
  // stake (net up = green). Net profit (returned − staked) is the revenue chart.
  const made = row.n_settled
    ? statBlock("Made", money(row.returned), row.profit >= 0 ? "pos" : "neg")
    : statBlock("Made", "—");
  stats.appendChild(made);
  stats.appendChild(statBlock(
    "Hit rate",
    row.hit_rate == null ? "—" : Math.round(row.hit_rate * 100) + "%"));
  stats.appendChild(statBlock("Bets", `${row.n_settled}/${row.n_bets}`));
  c.appendChild(stats);

  const best = el("div", "pc-best");
  if (row.best_bet) {
    best.appendChild(el("span", "pc-best-k", "★ Best call"));
    best.appendChild(el("span", null,
      `${row.best_bet.pick} in ${row.best_bet.match} → +${money(row.best_bet.profit)}`));
  } else {
    best.appendChild(el("span", "pc-best-k", "★ Best call"));
    best.appendChild(el("span", "muted", row.n_settled ? "no wins yet" : "awaiting first results"));
  }
  c.appendChild(best);

  // Click the card to drill into this player's full bet history.
  c.classList.add("expandable");
  c.dataset.player = row.player;
  const toggle = el("div", "pc-toggle");
  toggle.appendChild(el("span", "pc-toggle-t", `Bet history (${row.n_bets})`));
  toggle.appendChild(el("span", "pc-caret", "▾"));
  c.appendChild(toggle);
  c.appendChild(betsPanel(row.bets));
  if (openPlayers.has(row.player)) c.classList.add("open");
  return c;
}

function svgEl(tag, attrs) {
  const n = document.createElementNS(SVGNS, tag);
  for (const k in attrs) n.setAttribute(k, attrs[k]);
  return n;
}

function renderChart(bk) {
  const host = document.getElementById("chart");
  const legend = document.getElementById("chart-legend");
  legend.replaceChildren();
  if (!bk || !bk.n) {
    host.replaceChildren(el("p", "chart-empty",
      "The revenue curves draw here once matches are settled — kickoff is June 11."));
    return;
  }
  const W = 820, H = 360, padL = 56, padR = 16, padT = 14, padB = 30;
  const n = bk.n, series = bk.series;
  const players = ORDER.filter((p) => Array.isArray(series[p]) && series[p].length);

  let lo = 0, hi = 0;
  players.forEach((p) => series[p].forEach((v) => { lo = Math.min(lo, v); hi = Math.max(hi, v); }));
  if (hi === lo) hi = lo + 10;
  const padY = (hi - lo) * 0.08 || 1; lo -= padY; hi += padY;
  const X = (i) => padL + (n <= 1 ? (W - padL - padR) / 2 : (i / (n - 1)) * (W - padL - padR));
  const Y = (v) => padT + ((hi - v) / (hi - lo)) * (H - padT - padB);

  const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, role: "img",
    "aria-label": "Cumulative profit per player across settled matches" });

  // horizontal gridlines + ₪ labels (incl. a bold zero line)
  const ticks = [...new Set([hi, (hi + lo) / 2, 0, lo].map((v) => Math.round(v)))];
  ticks.forEach((v) => {
    if (v > hi || v < lo) return;
    const yy = Y(v);
    svg.appendChild(svgEl("line", { x1: padL, y1: yy, x2: W - padR, y2: yy,
      stroke: v === 0 ? "#2f3c5c" : "#1e2740", "stroke-width": v === 0 ? 1.5 : 1 }));
    const t = svgEl("text", { x: padL - 8, y: yy + 4, fill: "#93a2c4", "font-size": 12, "text-anchor": "end" });
    t.textContent = (v < 0 ? "−₪" : "₪") + Math.abs(v);
    svg.appendChild(t);
  });
  const xl = svgEl("text", { x: (padL + W - padR) / 2, y: H - 6, fill: "#93a2c4",
    "font-size": 12, "text-anchor": "middle" });
  xl.textContent = `matches played → (${n})`;
  svg.appendChild(xl);

  players.forEach((p) => {
    const pts = series[p].map((v, i) => `${X(i).toFixed(1)},${Y(v).toFixed(1)}`).join(" ");
    svg.appendChild(svgEl("polyline", { points: pts, fill: "none", stroke: META[p].color,
      "stroke-width": 2.4, "stroke-linejoin": "round", "stroke-linecap": "round" }));
    if (n <= 3) series[p].forEach((v, i) =>
      svg.appendChild(svgEl("circle", { cx: X(i), cy: Y(v), r: 3, fill: META[p].color })));
  });
  host.replaceChildren(svg);

  players.forEach((p) => {
    const item = el("span", "leg");
    const sw = el("span", "sw"); sw.style.background = META[p].color;
    item.append(sw, document.createTextNode(META[p].name));
    legend.appendChild(item);
  });
}

async function load() {
  let data;
  try {
    const res = await fetch(`${DATA_URL}?t=${Date.now()}`, { cache: "no-store" });
    if (!res.ok) throw new Error(res.status);
    data = await res.json();
  } catch (e) {
    document.getElementById("board").replaceChildren(
      el("p", "empty", "Couldn’t load the leaderboard yet."));
    return;
  }
  const rank = (p) => ORDER.indexOf(p);
  const board = (data.leaderboard || []).slice().sort((a, b) =>
    (b.profit - a.profit) || (rank(a.player) - rank(b.player)));

  const frag = document.createDocumentFragment();
  board.forEach((row, i) => frag.appendChild(card(row, i + 1)));
  document.getElementById("board").replaceChildren(frag);

  renderChart(data.bankroll);

  if (data.meta && data.meta.generated_at) {
    document.getElementById("updated").textContent =
      "Updated " + new Date(data.meta.generated_at).toLocaleString();
  }
}

// Toggle a player's bet-history panel (state survives the 60s re-render).
document.addEventListener("click", (e) => {
  const card = e.target.closest(".player-card.expandable");
  if (!card) return;
  const p = card.dataset.player;
  if (card.classList.toggle("open")) openPlayers.add(p);
  else openPlayers.delete(p);
});

load();
setInterval(load, POLL_MS);

// Re-fetch when the tab is shown/focused again: a backgrounded tab's poll timer
// gets throttled or frozen, so without this it can sit on a stale snapshot.
function refreshIfVisible() { if (!document.hidden) load(); }
document.addEventListener("visibilitychange", refreshIfVisible);
window.addEventListener("focus", refreshIfVisible);
window.addEventListener("pageshow", refreshIfVisible);
