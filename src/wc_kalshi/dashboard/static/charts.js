"use strict";
// Performance analytics: stat cards + equity curve + calibration reliability chart.
// Loaded after app.js; defines window.renderAnalytics(d) (called from render()) and
// runs its own slower loop to refresh the charts. Degrades gracefully if Chart.js
// (CDN) failed to load — the stat numbers still render.
(function () {
  const m2 = (x) => x == null ? "—" : (x >= 0 ? "+$" : "-$") + Math.abs(+x).toFixed(2);
  const pc = (x) => x == null ? "—" : Math.round(x * 100) + "%";
  const hasChart = () => typeof window.Chart !== "undefined";
  let built = false, eqChart = null, calChart = null;

  function ensureDOM() {
    if (built) return;
    built = true;
    const el = document.getElementById("analytics");
    if (!el) return;
    el.innerHTML = `
      <div class="lbl" style="margin-top:4px">📈 Performance</div>
      <div class="card">
        <div class="statgrid" style="grid-template-columns:repeat(auto-fit,minmax(120px,1fr))">
          <div class="metric"><span>equity</span><b id="m_eq">—</b></div>
          <div class="metric"><span>net P&L</span><b id="m_pnl">—</b></div>
          <div class="metric"><span>win rate</span><b id="m_wr">—</b></div>
          <div class="metric"><span>bets</span><b id="m_n">—</b></div>
          <div class="metric"><span>best</span><b id="m_best" class="pos">—</b></div>
          <div class="metric"><span>worst</span><b id="m_worst" class="neg">—</b></div>
        </div>
        <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:14px;margin-top:14px">
          <div><div class="lbl">Equity curve</div><div class="chartbox"><canvas id="equityCanvas"></canvas></div></div>
          <div><div class="lbl">Calibration · predicted vs actual</div><div class="chartbox"><canvas id="calibCanvas"></canvas></div></div>
        </div>
        <div id="chartnote" class="empty" hidden>Charts need an internet connection (Chart.js); the numbers above stay live.</div>
      </div>`;
    if (!hasChart()) { const n = document.getElementById("chartnote"); if (n) n.hidden = false; }
  }

  window.renderAnalytics = function (d) {
    ensureDOM();
    const p = d.portfolio || {}, s = d.stats || {};
    const pnl = (p.realized_pnl || 0) + (p.unrealized_pnl || 0);
    const set = (id, v, cls) => { const e = document.getElementById(id); if (e) { e.textContent = v; if (cls) e.className = cls; } };
    set("m_eq", p.equity == null ? "—" : "$" + (+p.equity).toFixed(2));
    set("m_pnl", m2(pnl), pnl >= 0 ? "pos" : "neg");
    set("m_wr", s.win_rate == null ? "—" : pc(s.win_rate));
    set("m_n", s.n_bets != null ? s.n_bets : "—");
    set("m_best", m2(s.best)); set("m_worst", m2(s.worst));
  };

  const css = (v, fb) => { try { return getComputedStyle(document.documentElement).getPropertyValue(v).trim() || fb; } catch (e) { return fb; } };

  async function fetchCharts() {
    if (!hasChart()) return;
    try {
      const eq = await (await fetch("/api/equity")).json();
      drawEquity(eq);
    } catch (e) {}
    try {
      const cal = await (await fetch("/api/calibration")).json();
      drawCalib((cal && cal.reliability) || []);
    } catch (e) {}
  }

  function drawEquity(points) {
    const cv = document.getElementById("equityCanvas"); if (!cv) return;
    const labels = points.map(p => (p.ts || "").slice(11, 19));
    const data = points.map(p => p.equity);
    const accent = "#7b6cff", grid = css("--line", "#21303f"), ink = css("--ink2", "#8da2b8");
    if (eqChart) { eqChart.data.labels = labels; eqChart.data.datasets[0].data = data; eqChart.update("none"); return; }
    eqChart = new Chart(cv, {
      type: "line",
      data: { labels, datasets: [{ data, borderColor: accent, backgroundColor: "rgba(123,108,255,.12)", fill: true, tension: .25, pointRadius: 0, borderWidth: 2 }] },
      options: { responsive: true, maintainAspectRatio: false, animation: false,
        plugins: { legend: { display: false } },
        scales: { x: { ticks: { color: ink, maxTicksLimit: 6 }, grid: { color: grid } },
                  y: { ticks: { color: ink }, grid: { color: grid } } } }
    });
  }

  function drawCalib(rel) {
    const cv = document.getElementById("calibCanvas"); if (!cv) return;
    const pts = rel.map(r => ({ x: r.mean_predicted, y: r.empirical_freq }));
    const grid = css("--line", "#21303f"), ink = css("--ink2", "#8da2b8");
    if (calChart) { calChart.data.datasets[0].data = pts; calChart.update("none"); return; }
    calChart = new Chart(cv, {
      data: {
        datasets: [
          { type: "scatter", data: pts, borderColor: "#34d99a", backgroundColor: "#34d99a", pointRadius: 5 },
          { type: "line", data: [{ x: 0, y: 0 }, { x: 1, y: 1 }], borderColor: ink, borderDash: [5, 5], pointRadius: 0, borderWidth: 1 },
        ]
      },
      options: { responsive: true, maintainAspectRatio: false, animation: false,
        plugins: { legend: { display: false } },
        scales: { x: { min: 0, max: 1, title: { display: true, text: "predicted", color: ink }, ticks: { color: ink }, grid: { color: grid } },
                  y: { min: 0, max: 1, title: { display: true, text: "actual", color: ink }, ticks: { color: ink }, grid: { color: grid } } } }
    });
  }

  // ---------- match drill-down ----------
  function ctxHtml(cx, home, away) {
    const inj = (a) => (a && a.length) ? a.join(", ") : "none";
    const xi = (a) => (a && a.length) ? a.join(" · ") : "—";
    const xiBlock = (cx.home_xi && cx.home_xi.length) ? `
      <div class="lbl" style="margin-top:10px">Starting XI</div>
      <div class="sub" style="font-size:11px">${home}: ${xi(cx.home_xi)}</div>
      <div class="sub" style="font-size:11px;margin-top:3px">${away}: ${xi(cx.away_xi)}</div>` : "";
    return `<div class="card" style="margin-top:12px">
      <div class="row"><span class="k">${home} formation</span><b>${cx.home_formation || "—"}</b></div>
      <div class="row"><span class="k">${away} formation</span><b>${cx.away_formation || "—"}</b></div>
      <div class="lbl" style="margin-top:8px">Injuries / out</div>
      <div class="sub" style="font-size:12px">${home}: ${inj(cx.home_injuries)}</div>
      <div class="sub" style="font-size:12px;margin-top:2px">${away}: ${inj(cx.away_injuries)}</div>
      ${xiBlock}</div>`;
  }
  let dlProb = null, dlXg = null, dlSeries = [], dlOut = "home";
  function destroyDl() { if (dlProb) { dlProb.destroy(); dlProb = null; } if (dlXg) { dlXg.destroy(); dlXg = null; } }

  window.openMatch = async function (mid, home, away) {
    const modal = document.getElementById("modal"), body = document.getElementById("modalBody");
    if (!modal || !body) return;
    body.innerHTML = `
      <div style="display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap">
        <div><div style="font-size:17px;font-weight:800">${home || ""} v ${away || ""}</div>
          <div class="sub" id="dlScore"></div></div>
        <div id="dlOutcomes"></div>
      </div>
      <div id="dlContext"></div>
      <div class="lbl" style="margin-top:14px">Model vs market — <span id="dlOutLabel">home</span></div>
      <div class="chartbox tall"><canvas id="dlProbCanvas"></canvas></div>
      <div class="lbl" style="margin-top:16px">Cumulative xG</div>
      <div class="chartbox"><canvas id="dlXgCanvas"></canvas></div>
      <div id="dlEmpty" class="empty" hidden>No time-series recorded for this match yet.</div>`;
    modal.hidden = false;
    destroyDl(); dlOut = "home";
    let hist;
    try { hist = await (await fetch(`/api/matches/${encodeURIComponent(mid)}/history`)).json(); }
    catch (e) { document.getElementById("dlEmpty").hidden = false; return; }
    dlSeries = (hist && hist.series) || [];
    const sc = document.getElementById("dlScore");
    if (sc && hist.teams) sc.textContent = `${hist.teams.score} · ${dlSeries.length} ticks`;
    const cxEl = document.getElementById("dlContext");
    if (cxEl && hist.context) cxEl.innerHTML = ctxHtml(hist.context, home || "Home", away || "Away");
    document.getElementById("dlOutcomes").innerHTML = ["home", "draw", "away"].map(o =>
      `<button class="step" style="width:auto;padding:0 12px;margin-left:6px" onclick="window._dlSelect('${o}')">${o}</button>`).join("");
    if (!dlSeries.length) { document.getElementById("dlEmpty").hidden = false; return; }
    if (!hasChart()) { document.getElementById("dlEmpty").hidden = false;
      document.getElementById("dlEmpty").textContent = "Charts need internet (Chart.js)."; return; }
    drawXg(); window._dlSelect(dlOut);
  };

  window._dlSelect = function (o) {
    dlOut = o;
    const lab = document.getElementById("dlOutLabel"); if (lab) lab.textContent = o;
    drawProb();
  };

  function dlLabels() { return dlSeries.map((s, i) => s.minute != null ? s.minute + "'" : String(i)); }
  function drawProb() {
    const cv = document.getElementById("dlProbCanvas"); if (!cv || !hasChart()) return;
    const labels = dlLabels();
    const model = dlSeries.map(s => (s.model || {})[dlOut]);
    const market = dlSeries.map(s => (s.market || {})[dlOut]);
    const grid = css("--line", "#21303f"), ink = css("--ink2", "#8da2b8");
    if (dlProb) dlProb.destroy();
    dlProb = new Chart(cv, {
      type: "line",
      data: { labels, datasets: [
        { label: "model", data: model, borderColor: "#7b6cff", backgroundColor: "rgba(123,108,255,.10)", fill: true, tension: .25, pointRadius: 0, borderWidth: 2 },
        { label: "market", data: market, borderColor: "#54a8ff", tension: .25, pointRadius: 0, borderWidth: 2, borderDash: [5, 4] }] },
      options: { responsive: true, maintainAspectRatio: false, animation: false,
        plugins: { legend: { labels: { color: ink } } },
        scales: { x: { ticks: { color: ink, maxTicksLimit: 8 }, grid: { color: grid } },
                  y: { min: 0, max: 1, ticks: { color: ink }, grid: { color: grid } } } }
    });
  }
  function drawXg() {
    const cv = document.getElementById("dlXgCanvas"); if (!cv || !hasChart()) return;
    const labels = dlLabels();
    const h = dlSeries.map(s => s.xg ? s.xg[0] : null), a = dlSeries.map(s => s.xg ? s.xg[1] : null);
    const grid = css("--line", "#21303f"), ink = css("--ink2", "#8da2b8");
    if (dlXg) dlXg.destroy();
    dlXg = new Chart(cv, {
      type: "line",
      data: { labels, datasets: [
        { label: "home xG", data: h, borderColor: "#34d99a", tension: .25, pointRadius: 0, borderWidth: 2 },
        { label: "away xG", data: a, borderColor: "#ffc24b", tension: .25, pointRadius: 0, borderWidth: 2 }] },
      options: { responsive: true, maintainAspectRatio: false, animation: false,
        plugins: { legend: { labels: { color: ink } } },
        scales: { x: { ticks: { color: ink, maxTicksLimit: 8 }, grid: { color: grid } },
                  y: { ticks: { color: ink }, grid: { color: grid } } } }
    });
  }

  function start() { ensureDOM(); fetchCharts(); setInterval(fetchCharts, 8000); }
  if (document.readyState !== "loading") start(); else document.addEventListener("DOMContentLoaded", start);
})();
