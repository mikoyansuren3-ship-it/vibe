"""Paper executor — simulates fills locally. The DEFAULT, safe execution target.

Fills are modelled against the latest market snapshot. Fill models:
  * ``cross_spread`` (default) — fills the whole order at our limit (ask for a buy,
    bid for a sell), consistent with how the edge detector already charged the spread.
  * ``midpoint`` — optimistic variant filling at the mid.
  * ``book`` — walks the real order-book depth (``no_depth`` for buys, ``yes_depth``
    for sells), so large orders eat through levels and pay size-dependent slippage, and
    can PARTIALLY fill when depth within the limit runs out. This is the realistic one.

The Kalshi fee is applied so paper P&L includes realistic costs.
"""

from __future__ import annotations

from ..fees import kalshi_fee
from ..models.schemas import MarketSnapshot, OrderAction
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

    def _fee(self, contracts: int, price_cents: int) -> float:
        return kalshi_fee(
            contracts,
            price_cents / 100.0,
            coefficient=self.fee_coefficient,
            maker_fraction=self.maker_fraction,
        )

    def _walk_levels(self, order: OrderRequest, market: MarketSnapshot) -> list[tuple[int, int]]:
        """Return [(price_cents_in_yes_terms, qty)] consuming book depth up to the
        order size and within the limit price. Empty list -> no usable depth."""
        remaining = order.contracts
        out: list[tuple[int, int]] = []
        if order.action is OrderAction.BUY:
            # Buy Yes = lift the No bids; yes_ask = 100 - no_bid_price. Best (cheapest)
            # yes ask first => highest no bid first.
            levels = sorted(market.no_depth, key=lambda b: b.price_cents, reverse=True)
            for lvl in levels:
                yes_price = 100 - lvl.price_cents
                if yes_price > order.limit_price_cents:  # beyond our limit
                    break
                take = min(remaining, lvl.size)
                if take <= 0:
                    continue
                out.append((yes_price, take))
                remaining -= take
                if remaining <= 0:
                    break
        else:
            # Sell Yes = hit the Yes bids; best (highest) bid first.
            levels = sorted(market.yes_depth, key=lambda b: b.price_cents, reverse=True)
            for lvl in levels:
                if lvl.price_cents < order.limit_price_cents:  # below our minimum
                    break
                take = min(remaining, lvl.size)
                if take <= 0:
                    continue
                out.append((lvl.price_cents, take))
                remaining -= take
                if remaining <= 0:
                    break
        return out

    def _place_book(self, order: OrderRequest, market: MarketSnapshot | None) -> OrderResult:
        # No depth available -> degrade to a single cross-spread fill at the limit.
        if market is None or (not market.yes_depth and not market.no_depth):
            return self._place_flat(order, market)
        levels = self._walk_levels(order, market)
        if not levels:
            return OrderResult(
                order.client_order_id, OrderStatus.ACCEPTED,
                exchange_order_id=f"paper-{order.client_order_id}",
                message="no marketable depth within limit",
            )
        fills = [
            Fill(
                client_order_id=order.client_order_id,
                match_id=order.match_id,
                market_ticker=order.market_ticker,
                action=order.action,
                contracts=qty,
                price_cents=price,
                fee=self._fee(qty, price),
            )
            for price, qty in levels
        ]
        filled = sum(f.contracts for f in fills)
        fee = sum(f.fee for f in fills)
        avg = sum(f.price_cents * f.contracts for f in fills) / filled
        status = OrderStatus.FILLED if filled >= order.contracts else OrderStatus.PARTIAL
        return OrderResult(
            client_order_id=order.client_order_id,
            status=status,
            filled_contracts=filled,
            avg_price_cents=avg,
            fee=fee,
            exchange_order_id=f"paper-{order.client_order_id}",
            message=f"paper book fill {order.action.value} {filled}/{order.contracts} avg {avg:.1f}c",
            fills=fills,
        )

    def _place_flat(self, order: OrderRequest, market: MarketSnapshot | None) -> OrderResult:
        price = max(1, min(99, self._fill_price_cents(order, market)))
        fee = self._fee(order.contracts, price)
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

    async def _place(self, order: OrderRequest, market: MarketSnapshot | None) -> OrderResult:
        if order.contracts <= 0:
            return OrderResult(order.client_order_id, OrderStatus.REJECTED, message="zero size")
        if self.fill_model == "book":
            return self._place_book(order, market)
        return self._place_flat(order, market)
