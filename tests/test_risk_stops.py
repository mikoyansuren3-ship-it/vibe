"""Position-level stop-loss: flatten a market that runs too far underwater."""

import pytest

from wc_kalshi.engine.builders import build_runtime
from wc_kalshi.engine.match_loop import TickProcessor
from wc_kalshi.models.schemas import MarketSnapshot, OrderAction, Outcome


@pytest.fixture
def rt(cfg):
    return build_runtime(cfg)


def _snap(ticker="KX-1", match_id="m1", bid=40, ask=42):
    return MarketSnapshot(
        market_ticker=ticker, match_id=match_id, outcome=Outcome.HOME,
        yes_bid=bid, yes_ask=ask,
    )


async def test_stop_flattens_losing_position(rt, match_factory):
    rt.cfg.risk.position_stop_loss = 0.25
    # Bought 100 Yes @ 80c (cost $80); market mid collapses to 0.40 => -$40 (-50%).
    rt.portfolio.apply_fill(
        match_id="m1", market_ticker="KX-1", outcome=Outcome.HOME,
        action=OrderAction.BUY, contracts=100, price_cents=80, fee=0.0,
    )
    rt.last_mids["KX-1"] = 0.40
    proc = TickProcessor(rt, trade=True, persist=False)
    match = match_factory(match_id="m1", minute=60)
    await proc._check_position_stops(match, [_snap()])
    pos = rt.portfolio.positions.get("KX-1")
    assert pos is None or pos.net_yes == 0  # flattened
    assert rt.portfolio.realized_pnl < 0  # the loss was realized


async def test_no_stop_when_within_tolerance(rt, match_factory):
    rt.cfg.risk.position_stop_loss = 0.25
    rt.portfolio.apply_fill(
        match_id="m1", market_ticker="KX-1", outcome=Outcome.HOME,
        action=OrderAction.BUY, contracts=100, price_cents=80, fee=0.0,
    )
    rt.last_mids["KX-1"] = 0.70  # only -$10 (-12.5%), inside the 25% stop
    proc = TickProcessor(rt, trade=True, persist=False)
    match = match_factory(match_id="m1", minute=60)
    await proc._check_position_stops(match, [_snap(bid=69, ask=71)])
    assert rt.portfolio.positions["KX-1"].net_yes == 100  # untouched


async def test_stop_disabled_by_default(rt, match_factory):
    assert rt.cfg.risk.position_stop_loss == 0.0
    rt.portfolio.apply_fill(
        match_id="m1", market_ticker="KX-1", outcome=Outcome.HOME,
        action=OrderAction.BUY, contracts=100, price_cents=80, fee=0.0,
    )
    rt.last_mids["KX-1"] = 0.01  # catastrophic, but stop disabled
    proc = TickProcessor(rt, trade=True, persist=False)
    match = match_factory(match_id="m1", minute=60)
    await proc._check_position_stops(match, [_snap(bid=1, ask=3)])
    assert rt.portfolio.positions["KX-1"].net_yes == 100  # no stop fired
