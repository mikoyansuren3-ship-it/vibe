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


async def test_rejected_result_is_not_cached_but_success_is():
    """A REJECTED place must stay retryable — a transient failure can't be allowed to poison
    the coid — while a committed (accepted/filled) place stays idempotent."""
    from wc_kalshi.execution.base import Executor, OrderResult

    class _Scripted(Executor):
        def __init__(self, statuses):
            super().__init__()
            self._statuses = list(statuses)
            self.calls = 0

        async def _place(self, order, market):
            self.calls += 1
            return OrderResult(order.client_order_id, self._statuses.pop(0))

    ex = _Scripted([OrderStatus.REJECTED, OrderStatus.FILLED])
    r1 = await ex.place(_order(coid="c1"))
    assert r1.status is OrderStatus.REJECTED and ex.calls == 1
    r2 = await ex.place(_order(coid="c1"))  # same coid retried — the reject was NOT cached
    assert r2.status is OrderStatus.FILLED and ex.calls == 2
    r3 = await ex.place(_order(coid="c1"))  # success IS cached — no third _place
    assert r3 is r2 and ex.calls == 2


def _deep_market():
    from wc_kalshi.models.schemas import BookLevel

    # Buying Yes lifts the No bids: no_depth [46->yes55 x5, 45->yes55? ] etc.
    return MarketSnapshot(
        market_ticker="t", match_id="m1", outcome=Outcome.HOME, yes_bid=49, yes_ask=51,
        yes_depth=[BookLevel(price_cents=49, size=5), BookLevel(price_cents=48, size=50)],
        no_depth=[BookLevel(price_cents=49, size=5), BookLevel(price_cents=48, size=50)],
    )


async def test_book_model_walks_levels_and_slips():
    """Buying more than the top level eats deeper, worse-priced levels."""
    ex = PaperExecutor(fill_model="book")
    # yes_ask at level0 = 100-49 = 51 (size 5), level1 = 100-48 = 52 (size 50).
    res = await ex.place(_order(action=OrderAction.BUY, price=52, n=20), _deep_market())
    assert res.status is OrderStatus.FILLED
    assert res.filled_contracts == 20
    # 5 @ 51c + 15 @ 52c => avg 51.75, strictly worse than the top-of-book 51c.
    assert abs(res.avg_price_cents - 51.75) < 1e-9
    assert len(res.fills) == 2


async def test_book_model_partial_fill_when_depth_insufficient():
    ex = PaperExecutor(fill_model="book")
    # limit 51 only crosses level0 (size 5); the rest is beyond the limit -> partial.
    res = await ex.place(_order(action=OrderAction.BUY, price=51, n=20), _deep_market())
    assert res.status is OrderStatus.PARTIAL
    assert res.filled_contracts == 5


async def test_book_model_falls_back_when_no_depth():
    ex = PaperExecutor(fill_model="book")
    res = await ex.place(_order(price=51, n=10), _market())  # _market has no depth
    assert res.status is OrderStatus.FILLED
    assert res.filled_contracts == 10
    assert res.avg_price_cents == 51
