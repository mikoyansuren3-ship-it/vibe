"""Dashboard app factory.

Reads the shared in-memory ``RuntimeState`` plus the live risk/portfolio/calibration
objects. In **advisory** mode it surfaces pending ``TradeProposal``s with Approve /
Reject controls; in **autonomous** mode it shows the same live view with trades the
engine placed itself. Also exposes a one-click kill switch.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

from ..engine import trading

if TYPE_CHECKING:
    from ..engine.builders import Runtime
    from ..engine.orchestrator import Orchestrator


def _json_safe(obj: Any) -> Any:
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    return obj


def create_app(rt: "Runtime", orchestrator: "Orchestrator | None" = None) -> FastAPI:
    app = FastAPI(title="World Cup × Kalshi — In-Play Edge", version="0.2.0")

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return _INDEX_HTML

    @app.get("/api/state")
    async def state() -> JSONResponse:
        data: dict[str, Any] = rt.state.to_json()
        data["decision_mode"] = rt.cfg.execution.decision_mode
        data["run_mode"] = rt.cfg.mode.value
        data["risk"] = rt.risk.snapshot()
        data["portfolio"] = rt.portfolio.snapshot(rt.last_mids)
        data["proposals"] = trading.proposals_view(rt)
        data["calibration"] = rt.calibration.metrics()
        return JSONResponse(_json_safe(data))

    @app.get("/api/proposals")
    async def proposals() -> JSONResponse:
        return JSONResponse(_json_safe(trading.proposals_view(rt)))

    @app.post("/api/proposals/{pid}/approve")
    async def approve(pid: str, contracts: int | None = None) -> JSONResponse:
        ok, message = await trading.execute_proposal(rt, pid, contracts=contracts)
        return JSONResponse({"ok": ok, "message": message})

    @app.post("/api/proposals/{pid}/reject")
    async def reject(pid: str) -> JSONResponse:
        return JSONResponse({"ok": trading.reject_proposal(rt, pid)})

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
  :root{--bg:#0a0e15;--card:#141c28;--card2:#0f1722;--ink:#eaf1f9;--mut:#8499ad;
        --line:#22303f;--grn:#37d99a;--grn2:#0e6b46;--red:#ff5d77;--red2:#7a2333;
        --amb:#ffc24b;--blu:#54a8ff;--accent:#7b6cff}
  *{box-sizing:border-box} html,body{margin:0}
  body{background:radial-gradient(1200px 600px at 80% -10%,#16213400,#0a0e15),var(--bg);
    color:var(--ink);font:14px/1.45 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif}
  a{color:var(--blu)}
  header{display:flex;align-items:center;gap:14px;padding:12px 22px;border-bottom:1px solid var(--line);
    position:sticky;top:0;background:rgba(10,14,21,.92);backdrop-filter:blur(8px);z-index:9;flex-wrap:wrap}
  h1{font-size:15px;margin:0;font-weight:700;letter-spacing:.2px}
  .sub{color:var(--mut);font-weight:500}
  .pill{padding:3px 10px;border-radius:999px;font-size:11px;font-weight:700;border:1px solid var(--line);text-transform:uppercase;letter-spacing:.4px}
  .ok{color:var(--grn);border-color:#1d5b40;background:#0e2a1d} .bad{color:var(--red);border-color:#6b2233;background:#2a0e15}
  .info{color:var(--blu);border-color:#244a6b;background:#0d2236} .acc{color:var(--accent);border-color:#3a3470;background:#191634}
  .spacer{flex:1}
  .stat{display:flex;flex-direction:column;line-height:1.1} .stat b{font-size:18px;font-weight:800} .stat span{font-size:10px;color:var(--mut);text-transform:uppercase;letter-spacing:.5px}
  .kill{background:#2a0e15;color:var(--red);border:1px solid #6b2233;padding:8px 14px;border-radius:9px;font-weight:800;cursor:pointer;letter-spacing:.3px}
  .kill:hover{background:#3a121c}
  .wrap{display:grid;grid-template-columns:1fr 330px;gap:18px;padding:18px 22px;max-width:1500px;margin:0 auto}
  @media(max-width:960px){.wrap{grid-template-columns:1fr}}
  .lbl{font-size:11px;color:var(--mut);text-transform:uppercase;letter-spacing:.6px;margin:2px 0 8px;font-weight:700}
  .card{background:linear-gradient(180deg,var(--card),var(--card2));border:1px solid var(--line);border-radius:14px;padding:15px}
  .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:14px}
  /* proposals */
  .prop{border:1px solid #3a3470;border-left:4px solid var(--accent);border-radius:14px;padding:15px;
    background:linear-gradient(180deg,#171530,#11101f);margin-bottom:14px}
  .prop .top{display:flex;justify-content:space-between;align-items:flex-start;gap:10px}
  .prop .matchup{font-weight:700} .prop .when{color:var(--mut);font-size:12px}
  .verb{font-size:17px;font-weight:800;margin:8px 0 2px}
  .verb .b{color:var(--grn)} .verb .f{color:var(--red)}
  .edgechip{font-size:12px;font-weight:800;padding:4px 9px;border-radius:8px;background:#10261c;color:var(--grn);border:1px solid #1d5b40;white-space:nowrap}
  .thesis{color:#cdd9e6;font-size:13px;margin:8px 0}
  .tiles{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin:10px 0}
  .tile{background:#0c1320;border:1px solid var(--line);border-radius:9px;padding:7px 9px}
  .tile span{font-size:9.5px;color:var(--mut);text-transform:uppercase;letter-spacing:.4px;display:block}
  .tile b{font-size:14px;font-weight:800}
  .btns{display:flex;gap:9px;margin-top:6px}
  .btn{flex:1;padding:10px;border-radius:10px;font-weight:800;cursor:pointer;border:1px solid;font-size:13px;letter-spacing:.3px}
  .approve{background:#0e6b46;border-color:#19a06b;color:#eafff5} .approve:hover{background:#13855a}
  .reject{background:#2a0e15;border-color:#7a2333;color:#ffd7de} .reject:hover{background:#3a121c}
  .btn:disabled{opacity:.5;cursor:default}
  .sizer{display:flex;align-items:center;gap:8px;margin:2px 0 6px}
  .step{width:30px;height:30px;border-radius:9px;border:1px solid var(--line);background:#0c1320;color:var(--ink);font-weight:800;font-size:16px;cursor:pointer}
  .step:hover{background:#16202e}
  .szin{width:72px;height:30px;background:#0c1320;border:1px solid var(--line);border-radius:9px;color:var(--ink);text-align:center;font-weight:800;font-size:14px}
  /* matches */
  .teams{font-weight:700;font-size:14px} .min{float:right;color:var(--mut);font-weight:600}
  .score{font-size:20px;font-weight:800;margin:3px 0}
  .bar{display:flex;height:16px;border-radius:5px;overflow:hidden;margin:3px 0;border:1px solid var(--line)}
  .seg{display:flex;align-items:center;justify-content:center;font-size:10px;font-weight:800;color:#06101a}
  .edge{font-size:12px;padding:1px 0} .act{color:var(--grn);font-weight:800}
  .row{display:flex;justify-content:space-between;margin:5px 0;font-size:13px}
  .k{color:var(--mut)} .pos{color:var(--grn)} .neg{color:var(--red)}
  table{width:100%;border-collapse:collapse;font-size:12px} td{padding:4px 6px;border-bottom:1px solid var(--line)}
  .feed div{padding:5px 2px;border-bottom:1px solid var(--line);font-size:12px}
  .feed .tg{display:inline-block;min-width:62px;color:var(--mut)}
  .empty{color:var(--mut);padding:10px 2px;font-size:13px}
  .flash{position:fixed;bottom:18px;left:50%;transform:translateX(-50%);background:#13855a;color:#fff;
    padding:10px 18px;border-radius:10px;font-weight:700;opacity:0;transition:opacity .2s;z-index:20}
</style></head>
<body>
<header>
  <h1>⚽ World Cup × Kalshi <span class="sub">In-Play Edge</span></h1>
  <span id="runmode" class="pill info">—</span>
  <span id="decision" class="pill acc">—</span>
  <span id="status" class="pill">—</span>
  <span class="spacer"></span>
  <div class="stat"><b id="equity">—</b><span>equity</span></div>
  <div class="stat"><b id="pnl">—</b><span>P&amp;L</span></div>
  <div class="stat"><b id="open">—</b><span>open risk</span></div>
  <button class="kill" onclick="kill()">■ KILL SWITCH</button>
</header>

<div class="wrap">
  <div>
    <div id="decisionsWrap">
      <div class="lbl">⚡ Pending decisions <span id="propcount" class="sub"></span></div>
      <div id="proposals"></div>
    </div>
    <div class="lbl" style="margin-top:18px">Active matches</div>
    <div id="matches" class="grid"></div>
  </div>
  <div>
    <div class="card"><div class="lbl">Risk &amp; guardrails</div><div id="risk"></div></div>
    <div class="card" style="margin-top:16px"><div class="lbl">Open positions</div>
      <table id="positions"><tbody></tbody></table></div>
    <div class="card" style="margin-top:16px"><div class="lbl">Activity</div><div id="feed" class="feed"></div></div>
    <div class="card" style="margin-top:16px"><div class="lbl">Model calibration</div><div id="calib"></div></div>
  </div>
</div>
<div id="flash" class="flash"></div>

<script>
const f=(x,d=2)=>x==null?'—':(+x).toFixed(d);
const money=x=>x==null?'—':(x>=0?'+$':'-$')+Math.abs(+x).toFixed(2);
const pct=x=>x==null?'—':(x*100).toFixed(0)+'%';
let busy={};
function flash(msg,bad){const e=document.getElementById('flash');e.textContent=msg;e.style.background=bad?'#7a2333':'#13855a';e.style.opacity=1;setTimeout(()=>e.style.opacity=0,2200);}
function bar(p,colors){if(!p)return '<div class="empty">no market quote</div>';
  const s=[['home',p.home],['draw',p.draw],['away',p.away]];
  return '<div class="bar">'+s.map((x,i)=>`<div class="seg" style="width:${(x[1]*100).toFixed(0)}%;background:${colors[i]}">${(x[1]*100).toFixed(0)}</div>`).join('')+'</div>';}
async function act(id,kind){
  if(busy[id])return;
  let url=`/api/proposals/${id}/${kind}`;
  if(kind==='approve'){const el=document.getElementById('sz-'+id); const n=el?Math.max(1,parseInt(el.value)||1):0; if(n)url+=`?contracts=${n}`;}
  busy[id]=1; render(window._last);
  try{const r=await (await fetch(url,{method:'POST'})).json();
    flash(kind==='approve'?(r.ok?'✓ Order placed':('✗ '+(r.message||'rejected'))):'Dismissed', kind==='approve'&&!r.ok);
  }catch(e){flash('request failed',true)}
  delete busy[id]; delete sizes[id]; tick();
}
async function kill(){if(!confirm('Engage KILL SWITCH? Halts all new trading immediately.'))return; await fetch('/api/kill',{method:'POST'}); flash('Kill switch engaged',true); tick();}
function outLabel(p){return p.outcome==='home'?p.home_team:p.outcome==='away'?p.away_team:'Draw';}
let sizes={};  // user-adjusted contract counts per proposal id (survive polls)
function adjust(id,delta){const el=document.getElementById('sz-'+id);if(!el)return;
  sizes[id]=Math.max(1,(parseInt(el.value)||1)+delta); el.value=sizes[id]; recalc(id);}
function recalc(id){const el=document.getElementById('sz-'+id);if(!el)return;
  const n=Math.max(1,parseInt(el.value)||1); sizes[id]=n;
  const cost=+el.dataset.cost, edge=+el.dataset.edge;
  const ev=document.getElementById('ev-'+id), ml=document.getElementById('ml-'+id);
  if(ev)ev.textContent=money(n*edge); if(ml)ml.textContent=money(-(n*cost));}
function propCard(p){
  const back=p.action==='buy';
  const n=sizes[p.id]!=null?sizes[p.id]:p.contracts;
  const step=Math.max(1,Math.round(p.contracts*0.1));
  return `<div class="prop">
    <div class="top">
      <div><div class="matchup">${p.home_team} v ${p.away_team}</div>
        <div class="when">${p.minute}' · ${p.score}</div></div>
      <div class="edgechip">edge ${(p.net_edge*100).toFixed(1)}%</div>
    </div>
    <div class="verb"><span class="${back?'b':'f'}">${back?'BACK':'FADE'} ${outLabel(p)}</span>
      <span class="sub" style="font-size:13px"> @ ${p.limit_price_cents}¢</span></div>
    <div class="thesis">${p.thesis||''}</div>
    <div class="sizer">
      <span class="sub">size</span>
      <button class="step" onclick="adjust('${p.id}',-${step})" aria-label="decrease">−</button>
      <input id="sz-${p.id}" class="szin" type="number" min="1" value="${n}"
        data-cost="${p.cost_per_contract}" data-edge="${p.net_edge}" oninput="recalc('${p.id}')">
      <button class="step" onclick="adjust('${p.id}',${step})" aria-label="increase">+</button>
      <span class="sub">contracts (cap applies)</span>
    </div>
    <div class="tiles">
      <div class="tile"><span>model</span><b>${pct(p.model_prob)}</b></div>
      <div class="tile"><span>market</span><b>${pct(p.market_prob)}</b></div>
      <div class="tile"><span>exp. value</span><b id="ev-${p.id}" class="pos">${money(n*p.net_edge)}</b></div>
      <div class="tile"><span>max loss</span><b id="ml-${p.id}" class="neg">${money(-(n*p.cost_per_contract))}</b></div>
    </div>
    <div class="btns">
      <button class="btn approve" ${busy[p.id]?'disabled':''} onclick="act('${p.id}','approve')">✓ Approve</button>
      <button class="btn reject" ${busy[p.id]?'disabled':''} onclick="act('${p.id}','reject')">✕ Reject</button>
    </div></div>`;
}
function render(d){
  if(!d)return; window._last=d;
  document.getElementById('runmode').textContent=d.run_mode||'paper';
  const dec=d.decision_mode||'autonomous';
  document.getElementById('decision').textContent=dec;
  const r=d.risk||{}, p=d.portfolio||{};
  const st=document.getElementById('status');
  st.textContent=r.kill_switch?'KILLED':(r.halted?'HALTED':'running');
  st.className='pill '+((!r.halted&&!r.kill_switch)?'ok':'bad');
  document.getElementById('equity').textContent=f(p.equity);
  const pnl=(p.realized_pnl||0)+(p.unrealized_pnl||0);
  const pe=document.getElementById('pnl');pe.textContent=money(pnl);pe.className=pnl>=0?'pos':'neg';
  document.getElementById('open').textContent='$'+f(r.total_open_exposure);
  // proposals
  const pend=(d.proposals&&d.proposals.pending)||[];
  document.getElementById('propcount').textContent=pend.length?`(${pend.length})`:'';
  const dw=document.getElementById('decisionsWrap');
  if(dec==='advisory'){ dw.style.display='block';
    document.getElementById('proposals').innerHTML=pend.length?pend.map(propCard).join('')
      :'<div class="card empty">No pending decisions. The engine will surface a card here when it finds an edge worth your call.</div>';
  } else { dw.style.display='none'; }
  // matches
  document.getElementById('matches').innerHTML=(d.matches||[]).map(m=>`
    <div class="card"><div class="teams">${m.home_team} v ${m.away_team}<span class="min">${m.minute}' ${m.period||''}</span></div>
    <div class="score">${m.score} <span class="sub" style="font-size:12px">xG ${f(m.xg&&m.xg[0],2)}–${f(m.xg&&m.xg[1],2)} · 🟥 ${(m.red_cards&&m.red_cards[0])||0}/${(m.red_cards&&m.red_cards[1])||0}</span></div>
    <div class="lbl" style="margin:8px 0 2px">Model 1X2</div>${bar(m.model,['#37d99a','#8499ad','#54a8ff'])}
    <div class="lbl" style="margin:6px 0 2px">Market 1X2</div>${bar(m.market,['#1d7a52','#55657a','#2d6aa6'])}
    ${(m.edges||[]).filter(e=>e.actionable).map(e=>`<div class="edge act">▲ ${e.outcome}: net ${(e.net_edge>=0?'+':'')}${f(e.net_edge,3)}</div>`).join('')}
    </div>`).join('')||'<div class="card empty">Waiting for live matches… (the engine polls the World Cup feed)</div>';
  // risk
  document.getElementById('risk').innerHTML=`
    <div class="row"><span class="k">realized P&L</span><span class="${(r.realized_pnl_today||0)>=0?'pos':'neg'}">${money(r.realized_pnl_today)}</span></div>
    <div class="row"><span class="k">unrealized</span><span class="${(r.unrealized_pnl||0)>=0?'pos':'neg'}">${money(r.unrealized_pnl)}</span></div>
    <div class="row"><span class="k">open exposure</span><span>$${f(r.total_open_exposure)}</span></div>
    <div class="row"><span class="k">fees paid</span><span>$${f(p.fees_paid)}</span></div>
    <div class="row"><span class="k">halted</span><span class="${r.halted?'neg':'pos'}">${r.halted?('YES — '+(r.halt_reason||'')):'no'}</span></div>`;
  const pos=(p.open_positions)||{};
  document.querySelector('#positions tbody').innerHTML=Object.keys(pos).length?
    Object.entries(pos).map(([t,v])=>`<tr><td>${t}</td><td>${v.yes||0}Y / ${v.no||0}N</td></tr>`).join('')
    :'<tr><td class="empty">none</td></tr>';
  document.getElementById('feed').innerHTML=(d.recent_decisions||[]).slice(0,16).map(x=>`<div><span class="tg">${(x.ts||'').slice(11,19)}</span>${x.kind||''}: ${x.message||''}</div>`).join('')||'<div class="empty">—</div>';
  const c=d.calibration||{};
  document.getElementById('calib').innerHTML=`
    <div class="row"><span class="k">settled matches</span><span>${c.n||0}</span></div>
    <div class="row"><span class="k">Brier</span><span>${f(c.brier,3)}</span></div>
    <div class="row"><span class="k">ECE</span><span>${f(c.ece,3)}</span></div>
    <div class="row"><span class="k">Kelly factor</span><span>${f(c.calibration_factor,2)}</span></div>`;
}
async function tick(){ try{render(await (await fetch('/api/state')).json())}catch(e){} }
tick(); setInterval(tick,1500);
</script>
</body></html>"""
