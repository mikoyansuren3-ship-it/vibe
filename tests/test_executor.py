"""Paper executor: fills + idempotency."""

from wc_kalshi.execution.base import OrderRequest, OrderStatus
from wc_kalshi.execution.paper import PaperExecutor
from wc_kalshi.models.schemas import MarketSnapshot, OrderAction, Outcome


def _order(coid="c1", action=OrderAction.BUY, price=50, n=10):
    return OrderRequest(
        match_id="m1",
        market_ticker="t",
        outcome=Outcome.HOME,
        action=action,
        contracts=n,
        limit_price_cents=price,
        cost_per_contract=price / 100.0,
        client_order_id=coid,
    )


def _market():
    return MarketSnapshot(market_ticker="t", match_id="m1", outcome=Outcome.HOME, yes_bid=49, yes_ask=51)


async def test_cross_spread_fills_at_limit():
    ex = PaperExecutor(fill_model="cross_spread")
    res = await ex.place(_order(price=51), _market())
    assert res.status is OrderStatus.FILLED
    assert res.filled_contracts == 10
    assert res.avg_price_cents == 51
    assert res.fee > 0


async def test_idempotent_same_client_order_id():
    ex = PaperExecutor()
    o = _order(coid="dup")
    r1 = await ex.place(o, _market())
    r2 = await ex.place(_order(coid="dup", n=999), _market())  # different size, same id
    assert r1 is r2  # cached; never double-fired
    assert r2.filled_contracts == 10


async def test_zero_size_rejected():
    ex = PaperExecutor()
    res = await ex.place(_order(n=0), _market())
    assert res.status is OrderStatus.REJECTED
