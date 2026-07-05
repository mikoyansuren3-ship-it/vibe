"""Execution interfaces + idempotency.

Orders carry a ``client_order_id``; executors cache results by that id so a retry
or a websocket reconnect can never double-fire the same order.
"""

from __future__ import annotations

import enum
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from ..models.schemas import MarketSnapshot, OrderAction, Outcome, Side
from ..util import new_id, utcnow


class OrderStatus(str, enum.Enum):
    FILLED = "filled"
    PARTIAL = "partial"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    CANCELED = "canceled"


@dataclass
class OrderRequest:
    match_id: str
    market_ticker: str
    outcome: Outcome
    action: OrderAction  # buy/sell YES
    contracts: int
    limit_price_cents: int
    cost_per_contract: float
    side: Side = Side.YES
    time_in_force: str = "ioc"
    client_order_id: str = field(default_factory=lambda: new_id("wck-"))
    ts: str = field(default_factory=lambda: utcnow().isoformat())


@dataclass
class Fill:
    client_order_id: str
    match_id: str
    market_ticker: str
    action: OrderAction
    contracts: int
    price_cents: int
    fee: float
    ts: str = field(default_factory=lambda: utcnow().isoformat())


@dataclass
class OrderResult:
    client_order_id: str
    status: OrderStatus
    filled_contracts: int = 0
    avg_price_cents: float = 0.0
    fee: float = 0.0
    exchange_order_id: str | None = None
    message: str = ""
    fills: list[Fill] = field(default_factory=list)

    @property
    def is_filled(self) -> bool:
        return self.status in {OrderStatus.FILLED, OrderStatus.PARTIAL} and self.filled_contracts > 0


class Executor(ABC):
    mode: str = "base"

    def __init__(self) -> None:
        self._seen: dict[str, OrderResult] = {}

    async def place(
        self, order: OrderRequest, market: MarketSnapshot | None = None
    ) -> OrderResult:
        """Idempotent place: a repeated client_order_id returns the cached result.

        Only committed outcomes (accepted/partial/filled) are cached. A REJECTED result is
        deliberately NOT cached — otherwise a transient failure (network blip, 5xx, timeout)
        would poison that coid and make it permanently unretryable for the rest of the
        session. Re-placing a genuinely rejected order is safe: the exchange dedupes on the
        same client_order_id, so a retry can't double-fire.
        """
        cached = self._seen.get(order.client_order_id)
        if cached is not None:
            return cached
        result = await self._place(order, market)
        if result.status in {OrderStatus.ACCEPTED, OrderStatus.PARTIAL, OrderStatus.FILLED}:
            self._seen[order.client_order_id] = result
        return result

    @abstractmethod
    async def _place(self, order: OrderRequest, market: MarketSnapshot | None) -> OrderResult:
        ...

    async def cancel(self, exchange_order_id: str) -> bool:
        """Cancel a resting order by exchange id. Returns True on success.

        Default no-op (paper IOC orders never rest); live executors override this.
        """
        return True

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
        """All fills currently known for a resting order, in YES terms.

        Used to book LATE fills — a resting limit order that fills seconds after placement
        (past the one-shot reconcile in ``_place``) would otherwise never enter the ledger.
        Default: none (paper/IOC orders never rest); live executors override to poll the
        exchange. Returns the full fill set for the order; the caller books only the
        increment beyond what it has already booked.
        """
        return []

    async def aclose(self) -> None:  # pragma: no cover - default
        return None
