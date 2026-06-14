"""Kalshi executor for demo & live.

Maps our buy/sell-Yes intent onto Kalshi orders. A SELL of Yes is placed as a BUY
of the No contract (no inventory required), matching the portfolio model. Orders
carry our ``client_order_id`` for idempotency. Precise fill reconciliation via the
fills websocket/positions endpoint is a documented next step; here we read the
create-order response and estimate the fill for marketable limit orders.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..fees import kalshi_fee
from ..logging_setup import get_logger
from ..models.schemas import MarketSnapshot, OrderAction
from .base import Executor, Fill, OrderRequest, OrderResult, OrderStatus

if TYPE_CHECKING:
    from ..ingestion.kalshi.client import KalshiClient

log = get_logger("execution.kalshi")

_FILLED_STATES = {"executed", "filled"}
_RESTING_STATES = {"resting", "pending", "open"}


class KalshiExecutor(Executor):
    def __init__(
        self,
        client: "KalshiClient",
        *,
        mode: str = "demo",
        order_type: str = "limit",
        fee_coefficient: float = 0.07,
    ) -> None:
        super().__init__()
        self.client = client
        self.mode = mode
        self.order_type = order_type
        self.fee_coefficient = fee_coefficient

    def _build_payload(self, order: OrderRequest) -> dict:
        payload: dict[str, object] = {
            "ticker": order.market_ticker,
            "client_order_id": order.client_order_id,
            "type": self.order_type,
            "count": order.contracts,
            "action": "buy",  # always a purchase; SELL Yes == BUY No
        }
        if order.action is OrderAction.BUY:
            payload["side"] = "yes"
            payload["yes_price"] = order.limit_price_cents
        else:
            payload["side"] = "no"
            payload["no_price"] = max(1, 100 - order.limit_price_cents)
        return payload

    async def _place(self, order: OrderRequest, market: MarketSnapshot | None) -> OrderResult:
        if order.contracts <= 0:
            return OrderResult(order.client_order_id, OrderStatus.REJECTED, message="zero size")
        payload = self._build_payload(order)
        try:
            resp = await self.client.create_order(payload)
        except Exception as exc:
            log.error("order placement failed", extra={"err": str(exc), "coid": order.client_order_id})
            return OrderResult(order.client_order_id, OrderStatus.REJECTED, message=str(exc))

        body = resp.get("order", resp)
        status_raw = str(body.get("status", "")).lower()
        exch_id = body.get("order_id") or body.get("id")
        filled = int(
            body.get("taker_fill_count")
            or body.get("filled_count")
            or (order.contracts if status_raw in _FILLED_STATES else 0)
        )
        fee = kalshi_fee(filled, order.limit_price_cents / 100.0, coefficient=self.fee_coefficient)

        if status_raw in _FILLED_STATES or filled >= order.contracts:
            status = OrderStatus.FILLED
        elif 0 < filled < order.contracts:
            status = OrderStatus.PARTIAL
        elif status_raw in _RESTING_STATES:
            status = OrderStatus.ACCEPTED
        else:
            status = OrderStatus.ACCEPTED

        fills = (
            [
                Fill(
                    client_order_id=order.client_order_id,
                    match_id=order.match_id,
                    market_ticker=order.market_ticker,
                    action=order.action,
                    contracts=filled,
                    price_cents=order.limit_price_cents,
                    fee=fee,
                )
            ]
            if filled > 0
            else []
        )
        return OrderResult(
            client_order_id=order.client_order_id,
            status=status,
            filled_contracts=filled,
            avg_price_cents=float(order.limit_price_cents),
            fee=fee,
            exchange_order_id=str(exch_id) if exch_id else None,
            message=f"kalshi {self.mode} order status={status_raw or 'unknown'}",
            fills=fills,
        )

    async def aclose(self) -> None:
        await self.client.aclose()
