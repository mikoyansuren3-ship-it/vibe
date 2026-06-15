"""Advisory decision path: proposals, approve/reject, expiry, and endpoints."""

from __future__ import annotations

import asyncio
from datetime import timedelta

from fastapi.testclient import TestClient

from wc_kalshi.dashboard.app import create_app
from wc_kalshi.engine import trading
from wc_kalshi.engine.builders import build_runtime
from wc_kalshi.engine.match_loop import MatchState, TickProcessor
from wc_kalshi.ingestion.football.simulated import simulate_full_match
from wc_kalshi.ingestion.kalshi.feed import SimulatedMarketFeed
from wc_kalshi.models.schemas import ProposalStatus
from wc_kalshi.util import utcnow


def _runtime(cfg, tmp_db):
    cfg = cfg.model_copy(deep=True)
    cfg.risk.max_daily_loss = 1e9  # don't let the halt interfere with these tests
    return build_runtime(cfg, db=tmp_db)


async def _seed_proposal(rt):
    """Run simulated matches through an advisory processor until a proposal queues."""
    proc = TickProcessor(rt, decision_mode="advisory")
    feed = SimulatedMarketFeed(seed=7)
    for seed in range(8):
        st = MatchState(f"adv-{seed}")
        for m in simulate_full_match(seed=seed, match_id=f"adv-{seed}"):
            await proc.process(m, await feed.snapshots_for_match(m), st)
            pending = [p for p in rt.proposals.values() if p.is_pending]
            if pending:
                return pending[0]
    raise AssertionError("no proposal was generated")


async def test_advisory_queues_proposals_without_executing(cfg, tmp_db):
    rt = _runtime(cfg, tmp_db)
    await _seed_proposal(rt)
    assert any(p.is_pending for p in rt.proposals.values())
    # advisory must NOT place any order
    assert rt.portfolio.fees_paid == 0.0
    assert not rt.portfolio.positions
    await rt.aclose()


async def test_proposal_has_decision_context(cfg, tmp_db):
    rt = _runtime(cfg, tmp_db)
    p = await _seed_proposal(rt)
    assert p.thesis and p.contracts > 0
    assert p.max_loss > 0 and p.max_gain > 0
    assert p.limit_price_cents >= 1
    await rt.aclose()


async def test_approving_executes_and_books(cfg, tmp_db):
    rt = _runtime(cfg, tmp_db)
    p = await _seed_proposal(rt)
    ok, msg = await trading.execute_proposal(rt, p.id, persist=False)
    assert ok, msg
    assert rt.proposals[p.id].status is ProposalStatus.EXECUTED
    assert rt.portfolio.fees_paid > 0  # a real (paper) fill happened
    assert rt.risk.positions  # position registered with the risk manager
    # idempotent-ish: re-approving an executed proposal is refused
    ok2, _ = await trading.execute_proposal(rt, p.id, persist=False)
    assert not ok2
    await rt.aclose()


async def test_rejecting_does_not_execute(cfg, tmp_db):
    rt = _runtime(cfg, tmp_db)
    p = await _seed_proposal(rt)
    assert trading.reject_proposal(rt, p.id) is True
    assert rt.proposals[p.id].status is ProposalStatus.REJECTED
    assert rt.portfolio.fees_paid == 0.0
    assert trading.reject_proposal(rt, p.id) is False  # already decided
    await rt.aclose()


async def test_proposals_expire(cfg, tmp_db):
    rt = _runtime(cfg, tmp_db)
    p = await _seed_proposal(rt)
    assert p.is_pending
    trading.expire_proposals(rt, now=utcnow() + timedelta(seconds=10_000))
    assert rt.proposals[p.id].status is ProposalStatus.EXPIRED
    # an expired proposal cannot be approved
    ok, _ = await trading.execute_proposal(rt, p.id, persist=False)
    assert not ok
    await rt.aclose()


async def test_autonomous_executes_and_makes_no_proposals(cfg, tmp_db):
    rt = _runtime(cfg, tmp_db)
    proc = TickProcessor(rt, decision_mode="autonomous")
    feed = SimulatedMarketFeed(seed=7)
    st = MatchState("au")
    filled = False
    for m in simulate_full_match(seed=2, match_id="au"):
        await proc.process(m, await feed.snapshots_for_match(m), st)
        if st.n_fills > 0:
            filled = True
            break
    assert filled and rt.portfolio.fees_paid > 0
    assert not rt.proposals  # autonomous never queues proposals
    await rt.aclose()


async def test_approve_with_size_override(cfg, tmp_db):
    rt = _runtime(cfg, tmp_db)
    p = await _seed_proposal(rt)
    small = max(1, p.contracts // 3)  # well under the caps -> not clamped
    ok, msg = await trading.execute_proposal(rt, p.id, contracts=small, persist=False)
    assert ok, msg
    pos = rt.portfolio.positions.get(p.market_ticker)
    booked = pos.yes_contracts if p.action.value == "buy" else pos.no_contracts
    assert booked == small  # executed the size we chose, not the proposed size
    await rt.aclose()


def test_dashboard_approve_with_size(cfg, tmp_db):
    rt = _runtime(cfg, tmp_db)
    asyncio.run(_seed_proposal(rt))
    client = TestClient(create_app(rt))
    pend = client.get("/api/proposals").json()["pending"]
    pid, proposed = pend[0]["id"], pend[0]["contracts"]
    small = max(1, proposed // 4)
    r = client.post(f"/api/proposals/{pid}/approve?contracts={small}").json()
    assert r["ok"] is True
    assert rt.portfolio.fees_paid > 0  # a fill happened at the chosen size


async def _settle_some(rt):
    """Run full autonomous matches until at least one bet settles into history."""
    proc = TickProcessor(rt, decision_mode="autonomous")
    feed = SimulatedMarketFeed(seed=7)
    for seed in range(6):
        st = MatchState(f"h-{seed}")
        for m in simulate_full_match(seed=seed, match_id=f"h-{seed}"):
            await proc.process(m, await feed.snapshots_for_match(m), st)
        if rt.bet_history:
            return


def test_bet_history_and_active_bets(cfg, tmp_db):
    rt = _runtime(cfg, tmp_db)
    asyncio.run(_settle_some(rt))
    assert rt.bet_history, "expected at least one settled bet in history"
    b = rt.bet_history[0]
    assert {"match", "label", "side", "contracts", "pnl", "won", "result"} <= set(b)

    client = TestClient(create_app(rt))
    s = client.get("/api/state").json()
    assert isinstance(s["bet_history"], list) and len(s["bet_history"]) > 0
    assert "active_bets" in s  # present (empty after settlement, populated mid-match)


def test_equity_curve_and_session_stats(cfg, tmp_db):
    rt = _runtime(cfg, tmp_db)
    asyncio.run(_settle_some(rt))  # full matches -> equity samples + settled bets
    assert len(rt.equity_curve) >= 1
    client = TestClient(create_app(rt))
    eq = client.get("/api/equity").json()
    assert isinstance(eq, list) and len(eq) >= 1 and "equity" in eq[0]
    stats = client.get("/api/state").json()["stats"]
    assert stats["n_bets"] >= 1 and "win_rate" in stats and "best" in stats


def test_dashboard_proposal_endpoints(cfg, tmp_db):
    rt = _runtime(cfg, tmp_db)
    asyncio.run(_seed_proposal(rt))
    client = TestClient(create_app(rt))

    pv = client.get("/api/proposals").json()
    assert pv["pending"], "expected a pending proposal in the API"
    pid = pv["pending"][0]["id"]

    state = client.get("/api/state").json()
    assert state["decision_mode"] in ("advisory", "autonomous")
    assert "proposals" in state

    rejected = client.post(f"/api/proposals/{pid}/reject").json()
    assert rejected["ok"] is True
    # now it's gone from pending
    assert all(p["id"] != pid for p in client.get("/api/proposals").json()["pending"])
