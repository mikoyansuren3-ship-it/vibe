"""Dashboard app factory.

Reads the shared in-memory ``RuntimeState`` (updated each tick) plus the live risk
and calibration objects. Exposes a one-click kill switch endpoint that flattens new
trading by engaging the risk kill switch and stopping the orchestrator.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

if TYPE_CHECKING:
    from ..engine.builders import Runtime
    from ..engine.orchestrator import Orchestrator


def _json_safe(obj: Any) -> Any:
    """Replace NaN/Inf with None (strict JSON) recursively."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    return obj


def create_app(rt: "Runtime", orchestrator: "Orchestrator | None" = None) -> FastAPI:
    app = FastAPI(title="World Cup × Kalshi — In-Play Edge", version="0.1.0")

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return _INDEX_HTML

    @app.get("/api/state")
    async def state() -> JSONResponse:
        data: dict[str, Any] = rt.state.to_json()
        data["risk"] = rt.risk.snapshot()
        data["portfolio"] = rt.portfolio.snapshot(rt.last_mids)
        return JSONResponse(_json_safe(data))

    @app.get("/api/calibration")
    async def calibration() -> JSONResponse:
        return JSONResponse(
            _json_safe(
                {"metrics": rt.calibration.metrics(), "reliability": rt.calibration.reliability_table()}
            )
        )

    @app.get("/api/health")
    async def health() -> JSONResponse:
        return JSONResponse({"ok": True, "mode": rt.cfg.mode.value})

    @app.post("/api/kill")
    async def kill() -> JSONResponse:
        reason = "kill switch engaged via dashboard"
        if orchestrator is not None:
            orchestrator.kill(reason)
        else:
            rt.risk.engage_kill_switch(reason)
        return JSONResponse({"ok": True, "kill_switch": True, "reason": reason})

    return app


_INDEX_HTML = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>World Cup × Kalshi — In-Play Edge</title>
<style>
  :root{--bg:#0c1118;--card:#151c27;--ink:#e7edf5;--mut:#8aa0b6;--line:#243140;
        --grn:#39d98a;--red:#ff5470;--amb:#ffb547;--blu:#4aa8ff}
  *{box-sizing:border-box} body{margin:0;background:var(--bg);color:var(--ink);
    font:14px/1.4 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif}
  header{display:flex;align-items:center;gap:16px;padding:12px 20px;border-bottom:1px solid var(--line);
    position:sticky;top:0;background:var(--bg);z-index:5;flex-wrap:wrap}
  h1{font-size:15px;margin:0;font-weight:600;letter-spacing:.3px}
  .pill{padding:3px 9px;border-radius:999px;font-size:12px;font-weight:600;border:1px solid var(--line)}
  .ok{color:var(--grn);border-color:#1d5b40} .bad{color:var(--red);border-color:#6b2233}
  .muted{color:var(--mut)} .grow{flex:1}
  .kill{margin-left:auto;background:#2a0e15;color:var(--red);border:1px solid #6b2233;
    padding:7px 14px;border-radius:8px;font-weight:700;cursor:pointer}
  .kill:hover{background:#3a121c}
  .wrap{display:grid;grid-template-columns:1fr 320px;gap:16px;padding:16px 20px}
  @media(max-width:880px){.wrap{grid-template-columns:1fr}}
  .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(330px,1fr));gap:14px}
  .card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px}
  .teams{font-weight:700;font-size:15px} .min{float:right;color:var(--mut);font-weight:600}
  .score{font-size:22px;font-weight:800;margin:4px 0}
  .row{display:flex;justify-content:space-between;margin:5px 0;font-size:13px}
  .bar{display:flex;height:18px;border-radius:5px;overflow:hidden;margin:3px 0;border:1px solid var(--line)}
  .seg{display:flex;align-items:center;justify-content:center;font-size:11px;font-weight:700;color:#0c1118}
  .lbl{font-size:11px;color:var(--mut);text-transform:uppercase;letter-spacing:.5px;margin-top:8px}
  .edge{font-size:12px;padding:2px 0} .act{color:var(--grn);font-weight:700}
  table{width:100%;border-collapse:collapse;font-size:12px} td,th{text-align:left;padding:4px 6px;border-bottom:1px solid var(--line)}
  .feed div{padding:5px 0;border-bottom:1px solid var(--line);font-size:12px}
  .k{color:var(--mut)} .big{font-size:20px;font-weight:800}
  .pos{color:var(--grn)} .neg{color:var(--red)}
</style></head>
<body>
<header>
  <h1>⚽ World Cup × Kalshi <span class="muted">In-Play Edge</span></h1>
  <span id="mode" class="pill">—</span>
  <span id="status" class="pill">—</span>
  <span class="muted">equity</span><span id="equity" class="big">—</span>
  <span class="muted">P&L</span><span id="pnl" class="big">—</span>
  <button class="kill" onclick="kill()">■ KILL SWITCH</button>
</header>
<div class="wrap">
  <div>
    <div class="lbl">Active matches</div>
    <div id="matches" class="grid"></div>
  </div>
  <div>
    <div class="card"><div class="lbl">Risk</div><div id="risk"></div></div>
    <div class="card" style="margin-top:14px"><div class="lbl">Open positions</div>
      <table id="positions"><tbody></tbody></table></div>
    <div class="card" style="margin-top:14px"><div class="lbl">Recent decisions</div>
      <div id="feed" class="feed"></div></div>
  </div>
</div>
<script>
const f=(x,d=2)=>x==null?'—':(+x).toFixed(d);
const money=x=>x==null?'—':(x>=0?'+':'')+(+x).toFixed(2);
function bar(p,colors){if(!p)return '<div class="muted" style="font-size:12px">no market</div>';
  const segs=[['home',p.home],['draw',p.draw],['away',p.away]];
  return '<div class="bar">'+segs.map((s,i)=>`<div class="seg" style="width:${(s[1]*100).toFixed(0)}%;background:${colors[i]}">${(s[1]*100).toFixed(0)}</div>`).join('')+'</div>';}
async function kill(){if(!confirm('Engage KILL SWITCH? This halts all new trading.'))return;
  await fetch('/api/kill',{method:'POST'});tick();}
async function tick(){
  let d; try{d=await (await fetch('/api/state')).json()}catch(e){return}
  document.getElementById('mode').textContent='mode: '+d.mode;
  const r=d.risk||{}; const st=document.getElementById('status');
  const allowed=r.trading_allowed;
  st.textContent=r.kill_switch?'KILL SWITCH':(r.halted?'HALTED':'trading');
  st.className='pill '+(allowed?'ok':'bad');
  const p=d.portfolio||{};
  document.getElementById('equity').textContent=f(p.equity);
  const pnl=(p.realized_pnl||0)+(p.unrealized_pnl||0);
  const pe=document.getElementById('pnl');pe.textContent=money(pnl);pe.className='big '+(pnl>=0?'pos':'neg');
  // matches
  document.getElementById('matches').innerHTML=(d.matches||[]).map(m=>`
    <div class="card"><div class="teams">${m.home_team} v ${m.away_team}<span class="min">${m.minute}' ${m.period||''}</span></div>
    <div class="score">${m.score} <span class="muted" style="font-size:12px">xG ${f(m.xg?.[0],2)}–${f(m.xg?.[1],2)} · 🟥 ${m.red_cards?.[0]||0}/${m.red_cards?.[1]||0}</span></div>
    <div class="lbl">Model 1X2</div>${bar(m.model,['#39d98a','#8aa0b6','#4aa8ff'])}
    <div class="lbl">Market 1X2</div>${bar(m.market,['#1d7a52','#55657a','#2d6aa6'])}
    <div class="lbl">Edges</div>${(m.edges||[]).map(e=>`<div class="edge ${e.actionable?'act':'muted'}">${e.outcome}: raw ${e.raw_edge>=0?'+':''}${f(e.raw_edge,3)} · net ${e.net_edge>=0?'+':''}${f(e.net_edge,3)} ${e.actionable?'◀ ACTIONABLE':''}</div>`).join('')}
    </div>`).join('')||'<div class="muted">waiting for live matches…</div>';
  // risk
  document.getElementById('risk').innerHTML=`
    <div class="row"><span class="k">realized P&L</span><span class="${(r.realized_pnl_today||0)>=0?'pos':'neg'}">${money(r.realized_pnl_today)}</span></div>
    <div class="row"><span class="k">unrealized</span><span class="${(r.unrealized_pnl||0)>=0?'pos':'neg'}">${money(r.unrealized_pnl)}</span></div>
    <div class="row"><span class="k">open exposure</span><span>${f(r.total_open_exposure)}</span></div>
    <div class="row"><span class="k">fees paid</span><span>${f(p.fees_paid)}</span></div>
    <div class="row"><span class="k">halted</span><span class="${r.halted?'neg':'pos'}">${r.halted?('YES — '+(r.halt_reason||'')):'no'}</span></div>`;
  // positions
  const pos=(p.open_positions)||{};
  document.querySelector('#positions tbody').innerHTML=Object.keys(pos).length?
    Object.entries(pos).map(([t,v])=>`<tr><td>${t}</td><td>${v.yes||v.net_yes||0} yes</td><td>${v.no||0} no</td></tr>`).join('')
    :'<tr><td class="muted">none</td></tr>';
  // feed
  document.getElementById('feed').innerHTML=(d.recent_decisions||[]).slice(0,18).map(x=>`<div><span class="k">${(x.ts||'').slice(11,19)}</span> ${x.kind||''}: ${x.message||''}</div>`).join('')||'<div class="muted">—</div>';
}
tick(); setInterval(tick,1500);
</script>
</body></html>"""
