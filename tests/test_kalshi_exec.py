"""Kalshi executor: real fill reconciliation (not limit-price fabrication)."""

from wc_kalshi.execution.base import OrderRequest, OrderStatus
from wc_kalshi.execution.kalshi_exec import KalshiExecutor
from wc_kalshi.models.schemas import OrderAction, Outcome


class FakeClient:
    def __init__(self, create_resp, fills_resp):
        self.create_resp = create_resp
        self.fills_resp = fills_resp
        self.fills_calls = []

    async def create_order(self, payload):
        return self.create_resp

    async def get_fills(self, *, ticker=None, order_id=None, limit=200, cursor=None):
        self.fills_calls.append((ticker, order_id))
        return self.fills_resp

    async def aclose(self):
        pass


def _order(action=OrderAction.BUY, n=10, price=50, coid="c1"):
    return OrderRequest(
        match_id="m1", market_ticker="t", outcome=Outcome.HOME, action=action,
        contracts=n, limit_price_cents=price, cost_per_contract=price / 100.0, client_order_id=coid,
    )


async def test_reconciles_real_fill_price_not_limit():
    # We sent a limit of 50c but actually filled across 48c/49c -> avg < 50.
    client = FakeClient(
        create_resp={"order": {"order_id": "X1", "status": "executed"}},
        fills_resp={"fills": [
            {"count": 6, "yes_price": 48, "is_taker": True},
            {"count": 4, "yes_price": 49, "is_taker": True},
        ]},
    )
    ex = KalshiExecutor(client, mode="demo")
    res = await ex.place(_order(price=50, n=10))
    assert res.status is OrderStatus.FILLED
    assert res.filled_contracts == 10
    assert abs(res.avg_price_cents - 48.4) < 1e-9  # (6*48 + 4*49)/10
    assert client.fills_calls == [("t", "X1")]


async def test_partial_fill_from_reconciliation():
    client = FakeClient(
        create_resp={"order": {"order_id": "X2", "status": "resting"}},
        fills_resp={"fills": [{"count": 3, "yes_price": 50, "is_taker": False}]},
    )
    ex = KalshiExecutor(client, mode="demo")
    res = await ex.place(_order(n=10))
    assert res.status is OrderStatus.PARTIAL
    assert res.filled_contracts == 3
    assert res.fee > 0  # maker fee computed from real price


async def test_sell_yes_translates_no_price_to_yes_terms():
    # SELL yes was placed as BUY no; a no_price of 55 == yes price 45.
    client = FakeClient(
        create_resp={"order": {"order_id": "X3", "status": "executed"}},
        fills_resp={"fills": [{"count": 5, "no_price": 55, "is_taker": True}]},
    )
    ex = KalshiExecutor(client, mode="demo")
    res = await ex.place(_order(action=OrderAction.SELL, n=5, price=44))
    assert res.filled_contracts == 5
    assert res.avg_price_cents == 45.0  # 100 - 55


async def test_fallback_to_estimate_when_fills_unavailable():
    class BoomClient(FakeClient):
        async def get_fills(self, **kw):
            raise RuntimeError("fills endpoint down")

    client = BoomClient(
        create_resp={"order": {"order_id": "X4", "status": "executed"}}, fills_resp={}
    )
    ex = KalshiExecutor(client, mode="demo")
    res = await ex.place(_order(price=50, n=10))
    assert res.status is OrderStatus.FILLED
    assert res.filled_contracts == 10
    assert res.avg_price_cents == 50.0  # fell back to the limit-price estimate
