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

  function start() { ensureDOM(); fetchCharts(); setInterval(fetchCharts, 8000); }
  if (document.readyState !== "loading") start(); else document.addEventListener("DOMContentLoaded", start);
})();
