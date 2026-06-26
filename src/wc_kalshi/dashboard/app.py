"""Dashboard app factory.

Serves the static front-end (``dashboard/static/``) and the JSON/stream API over the
shared in-memory ``Runtime``. In advisory mode it surfaces pending ``TradeProposal``s
with Approve/Reject; autonomous mode shows the same live view with self-placed trades.
Also exposes a one-click kill switch.
"""

from __future__ import annotations

import asyncio
import json
import math
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from ..engine import trading
from ..modeling.xg_proxy import observed_xg

if TYPE_CHECKING:
    from ..engine.builders import Runtime
    from ..engine.orchestrator import Orchestrator

STATIC_DIR = Path(__file__).parent / "static"


def _json_safe(obj: Any) -> Any:
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    return obj


def _active_bets(rt: "Runtime") -> list[dict[str, Any]]:
    """Open positions enriched with team labels + live unrealized P&L."""
    rows: list[dict[str, Any]] = []
    for ticker, pos in rt.portfolio.positions.items():
        held = pos.yes_contracts or pos.no_contracts
        if not held:
            continue
        m = rt.state.matches.get(pos.match_id, {})
        home, away = m.get("home_team", "?"), m.get("away_team", "?")
        ov = pos.outcome.value
        label = home if ov == "home" else away if ov == "away" else "Draw"
        back = pos.yes_contracts >= pos.no_contracts
        mid = rt.last_mids.get(ticker)
        value = pos.value_at(mid) if mid is not None else None
        rows.append(
            {
                "ticker": ticker,
                "match": f"{home} v {away}",
                "label": label,
                "side": "back" if back else "fade",
                "contracts": pos.yes_contracts if back else pos.no_contracts,
                "cost": round(pos.cost_paid, 2),
                "value": round(value, 2) if value is not None else None,
                "unrealized": round(value - pos.cost_paid, 2) if value is not None else None,
                "minute": m.get("minute"),
            }
        )
    rows.sort(key=lambda r: abs(r["unrealized"] or 0), reverse=True)
    return rows


def _match_history(rt: "Runtime", match_id: str) -> dict[str, Any]:
    """Per-tick time-series for a match: model + de-vigged market 1X2, score, cumulative xG.

    Built from persisted ``edge_signals`` (model+market per outcome) joined to
    ``match_snapshots`` (minute/score/xG) by second.
    """
    edges = rt.db.iter_edges(match_id)
    snaps = rt.db.iter_match_snapshots(match_id)
    snap_by_sec = {s.ts.replace(microsecond=0): s for s in snaps}

    groups: dict[Any, dict[str, dict[str, float]]] = {}
    order: list[Any] = []
    for e in edges:
        sec = e.ts.replace(microsecond=0)
        if sec not in groups:
            groups[sec] = {"model": {}, "market": {}}
            order.append(sec)
        groups[sec]["model"][e.outcome.value] = round(e.model_prob, 4)
        groups[sec]["market"][e.outcome.value] = round(e.market_prob, 4)

    series: list[dict[str, Any]] = []
    for sec in order:
        g = groups[sec]
        s = snap_by_sec.get(sec)
        series.append(
            {
                "minute": s.minute if s else None,
                "score": f"{s.home_score}-{s.away_score}" if s else None,
                "xg": [round(observed_xg(s.home) or 0.0, 2),
                       round(observed_xg(s.away) or 0.0, 2)] if s else None,
                "model": g["model"],
                "market": g["market"],
            }
        )
    teams = context = None
    if snaps:
        last = snaps[-1]
        teams = {"home": last.home_team, "away": last.away_team,
                 "score": f"{last.home_score}-{last.away_score}"}
        from ..engine.match_loop import match_context_view
        context = match_context_view(last)
    return {"match_id": match_id, "teams": teams, "context": context, "series": series}


def _session_stats(rt: "Runtime") -> dict[str, Any]:
    """Aggregate performance stats from settled bet history."""
    h = rt.bet_history
    n = len(h)
    pnls = [b.get("pnl", 0.0) for b in h]
    wins = sum(1 for b in h if b.get("won"))
    realized = sum(pnls)
    return {
        "n_bets": n,
        "wins": wins,
        "win_rate": round(wins / n, 3) if n else None,
        "realized": round(realized, 2),
        "avg_pnl": round(realized / n, 2) if n else None,
        "best": round(max(pnls), 2) if pnls else None,
        "worst": round(min(pnls), 2) if pnls else None,
    }


def create_app(rt: "Runtime", orchestrator: "Orchestrator | None" = None) -> FastAPI:
    app = FastAPI(title="World Cup × Kalshi — In-Play Edge", version="0.3.0")
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/state")
    async def state() -> JSONResponse:
        data: dict[str, Any] = rt.state.to_json()
        data["decision_mode"] = rt.cfg.execution.decision_mode
        data["run_mode"] = rt.cfg.mode.value
        data["risk"] = rt.risk.snapshot()
        data["portfolio"] = rt.portfolio.snapshot(rt.last_mids)
        data["proposals"] = trading.proposals_view(rt)
        data["calibration"] = rt.calibration.metrics()
        data["active_bets"] = _active_bets(rt)
        data["bet_history"] = list(reversed(rt.bet_history))[:25]
        data["stats"] = _session_stats(rt)
        return JSONResponse(_json_safe(data))

    @app.get("/api/equity")
    async def equity() -> JSONResponse:
        return JSONResponse(_json_safe(list(rt.equity_curve)))

    @app.get("/api/matches/{match_id}/history")
    async def match_history(match_id: str) -> JSONResponse:
        return JSONResponse(_json_safe(_match_history(rt, match_id)))

    @app.get("/api/stream")
    async def stream() -> StreamingResponse:
        async def gen():
            yield ": connected\n\n"
            agen = rt.bus.stream()
            try:
                while True:
                    try:
                        ev = await asyncio.wait_for(agen.__anext__(), timeout=15.0)
                    except asyncio.TimeoutError:
                        yield ": ping\n\n"  # keep-alive; EventSource ignores comments
                        continue
                    payload = {"type": ev.type.value, "match_id": ev.match_id, "ts": ev.ts}
                    yield f"data: {json.dumps(payload)}\n\n"
            finally:
                await agen.aclose()

        return StreamingResponse(gen(), media_type="text/event-stream")

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
