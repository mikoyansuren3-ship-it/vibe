"""Risk gate under the trade lock.

Matches are processed concurrently, so a caller-side ``pre_trade_check`` is only
advisory: between that check and lock acquisition another task can book fills that
change exposure, and two stale approvals could jointly breach the caps.
``place_and_book(risk_check=True)`` re-runs the check UNDER ``rt.trade_lock`` —
these tests pin that serialized invariant.
"""

from __future__ import annotations

import asyncio

from wc_kalshi.engine import trading
from wc_kalshi.engine.builders import build_runtime
from wc_kalshi.models.schemas import Outcome, OrderAction


def _runtime(cfg, tmp_db, *, total_cap: float):
    cfg = cfg.model_copy(deep=True)
    cfg.risk.max_total_open_exposure = total_cap
    cfg.risk.max_daily_loss = 1e9  # the halt is not under test here
    return build_runtime(cfg, db=tmp_db)


def _place(rt, match_id: str, ticker: str, contracts: int = 100):
    return trading.place_and_book(
        rt,
        coid=f"{match_id}:{ticker}:t",
        match_id=match_id,
        market_ticker=ticker,
        outcome=Outcome.HOME,
        action=OrderAction.BUY,
        contracts=contracts,
        limit_price_cents=50,
        cost_per_contract=0.5,
        snap=None,
        persist=False,
        risk_check=True,
    )


async def test_concurrent_orders_cannot_jointly_breach_total_cap(cfg, tmp_db):
    """Two $50 orders against a $50 total cap, fired concurrently: exactly one may
    fill. Caller-side checks would both approve (each sees zero exposure)."""
    rt = _runtime(cfg, tmp_db, total_cap=50.0)
    # Both orders pass an advisory (stale) check before either books — the race.
    for m, t in (("m1", "T1"), ("m2", "T2")):
        assert rt.risk.pre_trade_check(
            match_id=m, market_ticker=t, action=OrderAction.BUY,
            contracts=100, cost_per_contract=0.5, price=0.5,
        ).approved
    results = await asyncio.gather(_place(rt, "m1", "T1"), _place(rt, "m2", "T2"))

    filled = [r for r, _ in results if r.is_filled]
    rejected = [r for r, _ in results if not r.is_filled]
    assert len(filled) == 1 and len(rejected) == 1
    assert rejected[0].message.startswith("risk:")
    assert rt.risk.total_open_exposure <= 50.0 + 1e-9
    await rt.aclose()


async def test_second_order_is_clamped_to_remaining_room(cfg, tmp_db):
    """With $75 of room, the loser of the lock race is clamped to the $25 that the
    winner left, not filled at its stale-approved full size."""
    rt = _runtime(cfg, tmp_db, total_cap=75.0)
    results = await asyncio.gather(_place(rt, "m1", "T1"), _place(rt, "m2", "T2"))

    sizes = sorted(r.filled_contracts for r, _ in results)
    assert sizes == [50, 100]  # 100 @ $0.50 fills, the other clamps to $25 of room
    assert rt.risk.total_open_exposure <= 75.0 + 1e-9
    await rt.aclose()


async def test_rejected_under_lock_places_and_books_nothing(cfg, tmp_db):
    rt = _runtime(cfg, tmp_db, total_cap=50.0)
    await _place(rt, "m1", "T1")  # consumes the whole cap
    result, n_fills = await _place(rt, "m2", "T2")

    assert n_fills == 0 and not result.is_filled
    assert result.status.value == "rejected"
    assert "T2" not in rt.portfolio.positions  # no order reached the executor
    assert rt.risk.positions.get("T2", 0) == 0
    await rt.aclose()


async def test_mint_coid_is_unique_and_bounded(cfg, tmp_db):
    """Same-minute re-fires must not mint an identical coid — that collides on the unique
    OrderRow column and aborts the tick after the order was booked. Each is unique and stays
    within the exchange's 64-char id limit even from a very long base."""
    from wc_kalshi.engine.match_loop import TickProcessor

    rt = build_runtime(cfg, db=tmp_db)
    proc = TickProcessor(rt, trade=False, persist=False)
    base = "m1:KX-WORLD-CUP-VERY-LONG-EVENT-TICKER-HERE:45:buy"
    a, b = proc._mint_coid(base), proc._mint_coid(base)
    assert a != b  # distinct despite an identical base
    assert len(a) <= 60 and len(b) <= 60  # exchange coid budget respected
    over = "x" * 500
    assert proc._mint_coid(over) != proc._mint_coid(over)  # truncation keeps the unique suffix
    await rt.aclose()


async def test_persist_order_swallows_duplicate_coid(cfg, tmp_db):
    """A duplicate coid hits the unique OrderRow column. The order is already placed + booked,
    so the IntegrityError must be logged and swallowed — never raised to unwind the tick."""
    from wc_kalshi.engine.trading import _persist_order
    from wc_kalshi.execution.base import OrderRequest, OrderResult, OrderStatus
    from wc_kalshi.models.db import OrderRow

    rt = build_runtime(cfg, db=tmp_db)
    order = OrderRequest(
        match_id="m", market_ticker="t", outcome=Outcome.HOME, action=OrderAction.BUY,
        contracts=1, limit_price_cents=50, cost_per_contract=0.5, client_order_id="dup",
    )
    result = OrderResult("dup", OrderStatus.ACCEPTED)
    _persist_order(rt, order, result)
    _persist_order(rt, order, result)  # duplicate coid — must not raise
    with rt.db.session() as s:
        assert s.query(OrderRow).filter(OrderRow.client_order_id == "dup").count() == 1
    await rt.aclose()
