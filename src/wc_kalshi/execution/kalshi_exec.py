"""Kalshi executor for demo & live.

Maps our buy/sell-Yes intent onto Kalshi orders. A SELL of Yes is placed as a BUY
of the No contract (no inventory required), matching the portfolio model. Orders
carry our ``client_order_id`` for idempotency.

Fill reconciliation: after placing, we read the ACTUAL fills from the
``/portfolio/fills`` endpoint (real executed counts, prices and taker flag) and book
those, rather than assuming the whole order filled at our limit price. If the fills
endpoint is unavailable or lags, we fall back to the create-order response estimate
so the executor still degrades gracefully.
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
        maker_fraction: float = 0.25,
        reconcile_fills: bool = True,
    ) -> None:
        super().__init__()
        self.client = client
        self.mode = mode
        self.order_type = order_type
        self.fee_coefficient = fee_coefficient
        self.maker_fraction = maker_fraction
        self.reconcile_fills = reconcile_fills

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

        # Prefer REAL fills from the exchange over the create-order estimate.
        real_fills: list[Fill] | None = None
        if self.reconcile_fills and exch_id:
            real_fills = await self._fills_from_exchange(
                str(exch_id),
                market_ticker=order.market_ticker,
                action=order.action,
                match_id=order.match_id,
                client_order_id=order.client_order_id,
                fallback_price_cents=order.limit_price_cents,
            )

        if real_fills is not None:
            fills = real_fills
            filled = sum(f.contracts for f in fills)
            fee = sum(f.fee for f in fills)
            avg_price = (
                sum(f.price_cents * f.contracts for f in fills) / filled if filled else 0.0
            )
        else:
            # Fallback: estimate from the create-order response, at the limit price.
            filled = int(
                body.get("taker_fill_count")
                or body.get("filled_count")
                or (order.contracts if status_raw in _FILLED_STATES else 0)
            )
            fee = kalshi_fee(
                filled, order.limit_price_cents / 100.0, coefficient=self.fee_coefficient
            )
            avg_price = float(order.limit_price_cents)
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

        if filled >= order.contracts and filled > 0:
            status = OrderStatus.FILLED
        elif 0 < filled < order.contracts:
            status = OrderStatus.PARTIAL
        elif status_raw in _FILLED_STATES:
            status = OrderStatus.FILLED
        else:
            status = OrderStatus.ACCEPTED

        return OrderResult(
            client_order_id=order.client_order_id,
            status=status,
            filled_contracts=filled,
            avg_price_cents=avg_price,
            fee=fee,
            exchange_order_id=str(exch_id) if exch_id else None,
            message=f"kalshi {self.mode} order status={status_raw or 'unknown'}",
            fills=fills,
        )

    async def fills_for(
        self,
        exchange_order_id: str,
        *,
        market_ticker: str,
        action: OrderAction,
        match_id: str,
        client_order_id: str,
        fallback_price_cents: int,
    ) -> list[Fill]:
        """Poll the exchange for a resting order's current fills (see Executor.fills_for).
        Failure/None → empty (retry next sweep); never let a read error look like 'no fills'."""
        fills = await self._fills_from_exchange(
            exchange_order_id,
            market_ticker=market_ticker,
            action=action,
            match_id=match_id,
            client_order_id=client_order_id,
            fallback_price_cents=fallback_price_cents,
        )
        return fills or []

    async def _fills_from_exchange(
        self,
        exch_id: str,
        *,
        market_ticker: str,
        action: OrderAction,
        match_id: str,
        client_order_id: str,
        fallback_price_cents: int,
    ) -> list[Fill] | None:
        """Read actual fills for an order and convert to Yes-terms Fill objects.

        Returns None on any failure so a placement caller falls back to the estimate (the
        resting re-read treats None as 'nothing new this pass'). A SELL of Yes was placed as
        a BUY of No, so we translate the No fill price back to Yes terms (yes_price =
        100 - no_price) to keep the portfolio in one currency.
        """
        try:
            resp = await self.client.get_fills(order_id=exch_id, ticker=market_ticker)
        except Exception as exc:
            log.warning("fill reconciliation failed; using estimate", extra={"err": str(exc)})
            return None
        raw = resp.get("fills", []) or []
        out: list[Fill] = []
        for f in raw:
            count = int(f.get("count") or f.get("filled_count") or 0)
            if count <= 0:
                continue
            if action is OrderAction.BUY:
                price = f.get("yes_price")
                price = int(price) if price is not None else fallback_price_cents
            else:
                no_price = f.get("no_price")
                price = 100 - int(no_price) if no_price is not None else fallback_price_cents
            is_taker = bool(f.get("is_taker", True))
            fee = kalshi_fee(
                count,
                price / 100.0,
                coefficient=self.fee_coefficient,
                maker=not is_taker,
                maker_fraction=self.maker_fraction,
            )
            out.append(
                Fill(
                    client_order_id=client_order_id,
                    match_id=match_id,
                    market_ticker=market_ticker,
                    action=action,
                    contracts=count,
                    price_cents=int(price),
                    fee=fee,
                )
            )
        return out

    async def cancel(self, exchange_order_id: str) -> bool:
        try:
            await self.client.cancel_order(exchange_order_id)
            return True
        except Exception as exc:
            log.warning("order cancel failed", extra={"order_id": exchange_order_id, "err": str(exc)})
            return False

    async def aclose(self) -> None:
        await self.client.aclose()
