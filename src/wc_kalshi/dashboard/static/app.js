"use strict";
// ---------- helpers ----------
const $ = (id) => document.getElementById(id);
const f = (x, d = 2) => (x == null || isNaN(x)) ? "—" : (+x).toFixed(d);
const money = (x) => x == null ? "—" : (x >= 0 ? "+$" : "-$") + Math.abs(+x).toFixed(2);
const pct = (x) => x == null ? "—" : Math.round(x * 100) + "%";
const abbr = (t) => (t || "?").slice(0, 3).toUpperCase();
const POLL_MS = 4000;  // SSE drives liveness; poll is the steady fallback

// ---------- theme ----------
function initTheme() {
  const t = localStorage.getItem("wck_theme") || "dark";
  document.documentElement.setAttribute("data-theme", t);
  $("themeBtn").textContent = t === "dark" ? "🌙" : "☀️";
}
function toggleTheme() {
  const cur = document.documentElement.getAttribute("data-theme");
  const next = cur === "dark" ? "light" : "dark";
  document.documentElement.setAttribute("data-theme", next);
  localStorage.setItem("wck_theme", next);
  $("themeBtn").textContent = next === "dark" ? "🌙" : "☀️";
}

// ---------- sound ----------
let muted = localStorage.getItem("wck_muted") === "1";
function refreshSoundBtn() { $("soundBtn").textContent = muted ? "🔕" : "🔔"; }
function toggleSound() { muted = !muted; localStorage.setItem("wck_muted", muted ? "1" : "0"); refreshSoundBtn(); }
let actx;
function beep(freq = 660) {
  if (muted) return;
  try {
    actx = actx || new (window.AudioContext || window.webkitAudioContext)();
    const o = actx.createOscillator(), g = actx.createGain();
    o.frequency.value = freq; o.connect(g); g.connect(actx.destination);
    g.gain.setValueAtTime(0.06, actx.currentTime);
    g.gain.exponentialRampToValueAtTime(0.0001, actx.currentTime + 0.25);
    o.start(); o.stop(actx.currentTime + 0.25);
  } catch (e) {}
}

// ---------- toasts ----------
const TITLES = { goal: "GOAL", red_card: "RED CARD", divergence: "DIVERGENCE", guardrail: "GUARDRAIL",
  fill: "FILL", proposal: "NEW PROPOSAL", reject: "REJECTED" };
function toast(kind, msg) {
  const box = $("toasts");
  const el = document.createElement("div");
  el.className = "toast " + (kind || "");
  el.innerHTML = `<b>${TITLES[kind] || kind || "alert"}</b>${msg}`;
  box.appendChild(el);
  setTimeout(() => el.remove(), 6500);
  while (box.children.length > 5) box.firstChild.remove();
  if (kind === "goal") beep(720); else if (kind === "red_card" || kind === "guardrail") beep(360);
}
let seen = new Set();
let firstLoad = true;
function decisionKind(d) {
  const k = (d.kind || "").replace("alert:", "");
  return k;
}
function processActivity(decisions) {
  for (const d of decisions || []) {
    const key = (d.ts || "") + "|" + (d.message || "");
    if (seen.has(key)) continue;
    seen.add(key);
    if (!firstLoad) {
      const k = decisionKind(d);
      if (["goal", "red_card", "divergence", "guardrail", "fill", "proposal", "reject"].includes(k))
        toast(k, d.message || "");
    }
  }
  if (seen.size > 400) seen = new Set([...seen].slice(-200));
}

// ---------- heartbeat ----------
let lastUpdate = 0;
function tickHeartbeat() {
  const hb = $("heartbeat"), txt = $("hbtext");
  if (!lastUpdate) { hb.className = "heartbeat dead"; txt.textContent = "connecting…"; return; }
  const age = Math.round((Date.now() - lastUpdate) / 1000);
  hb.className = "heartbeat" + (age > 20 ? " dead" : age > 8 ? " stale" : "");
  txt.textContent = age <= 2 ? "live" : `updated ${age}s ago`;
}

// ---------- proposals ----------
let busy = {}, sizes = {};
function outLabel(p) { return p.outcome === "home" ? p.home_team : p.outcome === "away" ? p.away_team : "Draw"; }
function adjust(id, delta) {
  const el = $("sz-" + id); if (!el) return;
  sizes[id] = Math.max(1, (parseInt(el.value) || 1) + delta); el.value = sizes[id]; recalc(id);
}
function recalc(id) {
  const el = $("sz-" + id); if (!el) return;
  const n = Math.max(1, parseInt(el.value) || 1); sizes[id] = n;
  const cost = +el.dataset.cost, edge = +el.dataset.edge;
  const ev = $("ev-" + id), ml = $("ml-" + id);
  if (ev) ev.textContent = money(n * edge);
  if (ml) ml.textContent = money(-(n * cost));
}
function propCard(p) {
  const back = p.action === "buy";
  const n = sizes[p.id] != null ? sizes[p.id] : p.contracts;
  const step = Math.max(1, Math.round(p.contracts * 0.1));
  return `<div class="prop">
    <div class="top">
      <div><div class="matchup">${p.home_team} v ${p.away_team}</div><div class="when">${p.minute}' · ${p.score}</div></div>
      <div class="edgechip">edge ${(p.net_edge * 100).toFixed(1)}%</div>
    </div>
    <div class="verb"><span class="${back ? "b" : "f"}">${back ? "BACK" : "FADE"} ${outLabel(p)}</span>
      <span class="sub" style="font-size:13px"> @ ${p.limit_price_cents}¢</span></div>
    <div class="thesis">${p.thesis || ""}</div>
    <div class="sizer"><span class="sub">size</span>
      <button class="step" onclick="adjust('${p.id}',-${step})" aria-label="decrease">−</button>
      <input id="sz-${p.id}" class="szin" type="number" min="1" value="${n}" data-cost="${p.cost_per_contract}" data-edge="${p.net_edge}" oninput="recalc('${p.id}')">
      <button class="step" onclick="adjust('${p.id}',${step})" aria-label="increase">+</button>
      <span class="sub">contracts (cap applies)</span></div>
    <div class="tiles">
      <div class="tile"><span>model</span><b>${pct(p.model_prob)}</b></div>
      <div class="tile"><span>market</span><b>${pct(p.market_prob)}</b></div>
      <div class="tile"><span>exp. value</span><b id="ev-${p.id}" class="pos">${money(n * p.net_edge)}</b></div>
      <div class="tile"><span>max loss</span><b id="ml-${p.id}" class="neg">${money(-(n * p.cost_per_contract))}</b></div>
    </div>
    <div class="btns">
      <button class="btn approve" ${busy[p.id] ? "disabled" : ""} onclick="act('${p.id}','approve')">✓ Approve</button>
      <button class="btn reject" ${busy[p.id] ? "disabled" : ""} onclick="act('${p.id}','reject')">✕ Reject</button>
    </div></div>`;
}
async function act(id, kind) {
  if (busy[id]) return;
  let url = `/api/proposals/${id}/${kind}`;
  if (kind === "approve") { const el = $("sz-" + id); const n = el ? Math.max(1, parseInt(el.value) || 1) : 0; if (n) url += `?contracts=${n}`; }
  busy[id] = 1;
  try {
    const r = await (await fetch(url, { method: "POST" })).json();
    toast(kind === "approve" ? "fill" : "reject", kind === "approve" ? (r.ok ? "Order placed" : ("Rejected: " + (r.message || ""))) : "Dismissed");
  } catch (e) { toast("guardrail", "request failed"); }
  delete busy[id]; delete sizes[id]; tick();
}
window.adjust = adjust; window.recalc = recalc; window.act = act;

// ---------- bars ----------
function bar(p, colors) {
  if (!p) return '<div class="empty">no market quote</div>';
  const s = [p.home, p.draw, p.away];
  return '<div class="bar">' + s.map((v, i) => `<div class="seg" style="width:${(v * 100).toFixed(0)}%;background:${colors[i]}">${(v * 100).toFixed(0)}</div>`).join("") + "</div>";
}

// ---------- render ----------
let lastState = null;
function render(d) {
  lastState = d;
  $("runmode").textContent = d.run_mode || "paper";
  $("decision").textContent = d.decision_mode || "autonomous";
  const r = d.risk || {}, p = d.portfolio || {};
  const st = $("status");
  st.textContent = r.kill_switch ? "KILLED" : (r.halted ? "HALTED" : "running");
  st.className = "pill " + ((!r.halted && !r.kill_switch) ? "ok" : "bad");
  $("equity").textContent = f(p.equity);
  const pnl = (p.realized_pnl || 0) + (p.unrealized_pnl || 0);
  $("pnl").textContent = money(pnl); $("pnl").className = pnl >= 0 ? "pos" : "neg";
  $("open").textContent = "$" + f(r.total_open_exposure);

  processActivity(d.recent_decisions);

  // proposals
  const dec = d.decision_mode || "autonomous";
  const pend = (d.proposals && d.proposals.pending) || [];
  $("decisionsWrap").hidden = dec !== "advisory";
  $("propcount").textContent = pend.length ? `(${pend.length})` : "";
  if (dec === "advisory")
    $("proposals").innerHTML = pend.length ? pend.map(propCard).join("")
      : '<div class="card empty">No pending decisions. A card appears here when the engine finds an edge worth your call.</div>';

  // matches
  $("matches").innerHTML = (d.matches || []).map(m => `
    <div class="card match" data-mid="${m.match_id}" onclick="window.openMatch&&window.openMatch('${m.match_id}','${(m.home_team||'').replace(/'/g,'')}','${(m.away_team||'').replace(/'/g,'')}')">
      <div class="teams">${m.home_team} v ${m.away_team}<span class="min">${m.minute}' ${m.period || ""}</span></div>
      <div class="score">${m.score} <span class="sub" style="font-size:12px">xG ${f(m.xg && m.xg[0], 2)}–${f(m.xg && m.xg[1], 2)} · 🟥 ${(m.red_cards && m.red_cards[0]) || 0}/${(m.red_cards && m.red_cards[1]) || 0}</span></div>
      <div class="lbl" style="margin:9px 0 2px">Model 1X2</div>${bar(m.model, ["#34d99a", "#8499ad", "#54a8ff"])}
      <div class="lbl" style="margin:6px 0 2px">Market 1X2</div>${bar(m.market, ["#1d7a52", "#55657a", "#2d6aa6"])}
      <div class="barlbl"><span>${abbr(m.home_team)}</span><span>DRAW</span><span>${abbr(m.away_team)}</span></div>
      ${(m.edges || []).filter(e => e.actionable).map(e => `<div class="act">▲ ${e.outcome}: net ${e.net_edge >= 0 ? "+" : ""}${f(e.net_edge, 3)}</div>`).join("")}
    </div>`).join("") || '<div class="card empty">Waiting for live matches… the engine polls the World Cup feed.</div>';

  // active bets
  const ab = d.active_bets || [];
  $("abcount").textContent = ab.length ? `(${ab.length})` : "";
  $("activebets").innerHTML = ab.length ? ab.map(b => `
    <div class="betrow">
      <div><b>${b.side === "back" ? "Back" : "Fade"} ${b.label}</b> <span class="sub">· ${b.contracts}${b.minute != null ? " · " + b.minute + "'" : ""}</span>
        <div class="sub" style="font-size:11px">${b.match}</div></div>
      <div style="text-align:right"><span class="${(b.unrealized || 0) >= 0 ? "pos" : "neg"}">${b.unrealized == null ? "—" : money(b.unrealized)}</span>
        <div class="sub" style="font-size:11px">${b.value == null ? "cost $" + f(b.cost) : "val $" + f(b.value)}</div></div>
    </div>`).join("") : '<div class="empty">no open bets</div>';

  // history
  const hist = d.bet_history || [];
  const tot = hist.reduce((s, b) => s + (b.pnl || 0), 0);
  $("histpnl").textContent = hist.length ? `· realized ${money(tot)}` : "";
  $("history").innerHTML = hist.length ? '<table><tbody>' + hist.map(b => `
    <tr><td><b>${b.side === "back" ? "Back" : "Fade"} ${b.label}</b> <span class="sub">· ${b.contracts}</span>
      <div class="sub" style="font-size:11px">${b.result}</div></td>
    <td style="text-align:right;white-space:nowrap"><span class="pillsm ${b.won ? "ok" : "bad"}">${b.won ? "WON" : "LOST"}</span>
      <span class="${b.pnl >= 0 ? "pos" : "neg"}" style="margin-left:7px">${money(b.pnl)}</span>
      <div class="sub" style="font-size:11px">${(b.ts || "").slice(11, 19)}</div></td></tr>`).join("") + "</tbody></table>"
    : '<div class="empty">No settled bets yet — they appear here when a match finishes.</div>';

  // risk
  $("risk").innerHTML = `
    <div class="row"><span class="k">realized P&L</span><span class="${(r.realized_pnl_today || 0) >= 0 ? "pos" : "neg"}">${money(r.realized_pnl_today)}</span></div>
    <div class="row"><span class="k">unrealized</span><span class="${(r.unrealized_pnl || 0) >= 0 ? "pos" : "neg"}">${money(r.unrealized_pnl)}</span></div>
    <div class="row"><span class="k">open exposure</span><span>$${f(r.total_open_exposure)}</span></div>
    <div class="row"><span class="k">fees paid</span><span>$${f(p.fees_paid)}</span></div>
    <div class="row"><span class="k">halted</span><span class="${r.halted ? "neg" : "pos"}">${r.halted ? "YES — " + (r.halt_reason || "") : "no"}</span></div>`;

  // feed
  $("feed").innerHTML = (d.recent_decisions || []).slice(0, 16).map(x => `<div><span class="tg">${(x.ts || "").slice(11, 19)}</span>${x.kind || ""}: ${x.message || ""}</div>`).join("") || '<div class="empty">—</div>';

  // calibration (numbers; chart added in analytics phase)
  const c = d.calibration || {};
  $("calib").innerHTML = `
    <div class="row"><span class="k">settled matches</span><span>${c.n || 0}</span></div>
    <div class="row"><span class="k">Brier</span><span>${f(c.brier, 3)}</span></div>
    <div class="row"><span class="k">ECE</span><span>${f(c.ece, 3)}</span></div>
    <div class="row"><span class="k">Kelly factor</span><span>${f(c.calibration_factor, 2)}</span></div>`;

  if (window.renderAnalytics) window.renderAnalytics(d);
  firstLoad = false;
}

async function tick() {
  try {
    const d = await (await fetch("/api/state")).json();
    lastUpdate = Date.now();
    render(d);
  } catch (e) {}
  tickHeartbeat();
}
async function kill() {
  if (!confirm("Engage KILL SWITCH? Halts all new trading immediately.")) return;
  await fetch("/api/kill", { method: "POST" }); toast("guardrail", "Kill switch engaged"); tick();
}

// ---------- boot ----------
function boot() {
  initTheme(); refreshSoundBtn();
  $("themeBtn").onclick = toggleTheme;
  $("soundBtn").onclick = toggleSound;
  $("killBtn").onclick = kill;
  $("modalClose").onclick = () => $("modal").hidden = true;
  $("modal").onclick = (e) => { if (e.target.id === "modal") $("modal").hidden = true; };
  tick();
  setInterval(tick, POLL_MS);
  setInterval(tickHeartbeat, 1000);
  initSSE();
}

// Server-sent events: instant refresh on engine events; poll remains the fallback.
let _sseDebounce = null;
function initSSE() {
  try {
    const es = new EventSource("/api/stream");
    es.onmessage = () => {
      lastUpdate = Date.now();
      if (_sseDebounce) return;
      _sseDebounce = setTimeout(() => { _sseDebounce = null; tick(); }, 400);
    };
    es.onerror = () => {};  // EventSource auto-reconnects; polling covers gaps
  } catch (e) {}
}
if (document.readyState !== "loading") boot(); else document.addEventListener("DOMContentLoaded", boot);
