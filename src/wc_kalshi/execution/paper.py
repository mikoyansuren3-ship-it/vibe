"""Paper executor — simulates fills locally. The DEFAULT, safe execution target.

Fills are modelled against the latest market snapshot. Default ``cross_spread``
fills at our own limit (the ask for a buy, the bid for a sell), consistent with how
the edge detector already charged the spread. ``midpoint`` is an optimistic variant.
The Kalshi fee is applied so paper P&L includes realistic costs.
"""

from __future__ import annotations

from ..fees import kalshi_fee
from ..models.schemas import MarketSnapshot
from .base import Executor, Fill, OrderRequest, OrderResult, OrderStatus


class PaperExecutor(Executor):
    mode = "paper"

    def __init__(
        self,
        *,
        fill_model: str = "cross_spread",
        fee_coefficient: float = 0.07,
        maker_fraction: float = 0.25,
    ) -> None:
        super().__init__()
        self.fill_model = fill_model
        self.fee_coefficient = fee_coefficient
        self.maker_fraction = maker_fraction

    def _fill_price_cents(self, order: OrderRequest, market: MarketSnapshot | None) -> int:
        if self.fill_model == "midpoint" and market is not None and market.yes_mid_cents:
            return int(round(market.yes_mid_cents))
        # cross_spread: take at our limit (ask for buy, bid for sell).
        return order.limit_price_cents

    async def _place(self, order: OrderRequest, market: MarketSnapshot | None) -> OrderResult:
        if order.contracts <= 0:
            return OrderResult(order.client_order_id, OrderStatus.REJECTED, message="zero size")

        price = self._fill_price_cents(order, market)
        price = max(1, min(99, price))
        # Fee is symmetric in p, so the YES price works for buying No too.
        fee = kalshi_fee(
            order.contracts,
            price / 100.0,
            coefficient=self.fee_coefficient,
            maker_fraction=self.maker_fraction,
        )
        fill = Fill(
            client_order_id=order.client_order_id,
            match_id=order.match_id,
            market_ticker=order.market_ticker,
            action=order.action,
            contracts=order.contracts,
            price_cents=price,
            fee=fee,
        )
        return OrderResult(
            client_order_id=order.client_order_id,
            status=OrderStatus.FILLED,
            filled_contracts=order.contracts,
            avg_price_cents=float(price),
            fee=fee,
            exchange_order_id=f"paper-{order.client_order_id}",
            message=f"paper fill {order.action.value} {order.contracts}@{price}c",
            fills=[fill],
        )
