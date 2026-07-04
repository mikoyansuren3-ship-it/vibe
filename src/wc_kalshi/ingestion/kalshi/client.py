"""Async Kalshi REST client.

Wraps the endpoints from research.md §1.3. Signs every request when a signer is
configured (market-data GETs also work unauthenticated on some deployments, so the
client degrades gracefully when no credentials are supplied — it just sends
unsigned GETs and raises clearly on write attempts).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx

from ...logging_setup import get_logger
from ..http import request_with_retry
from .auth import KalshiSigner

if TYPE_CHECKING:
    from ..budget import RequestBudget

log = get_logger("kalshi.client")


class KalshiAPIError(RuntimeError):
    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"Kalshi API error {status}: {body[:300]}")
        self.status = status
        self.body = body


class KalshiClient:
    def __init__(
        self,
        base_url: str,
        *,
        signer: KalshiSigner | None = None,
        timeout: float = 10.0,
        max_retries: int = 4,
        read_limiter: "RequestBudget | None" = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.signer = signer
        self.max_retries = max_retries
        # Paces GET reads only (market/orderbook fan-out) so parallel polls stay under the
        # exchange's read tier. Order placement (POST/DELETE) bypasses it — never delayed.
        self._read_limiter = read_limiter
        self._client = httpx.AsyncClient(timeout=timeout)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "KalshiClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # -- internals ------------------------------------------------------- #
    def _sign(self, method: str, url: str) -> dict[str, str]:
        if self.signer is None:
            return {}
        return self.signer.headers_for_url(method, url)

    def _require_auth(self) -> None:
        if self.signer is None:
            raise KalshiAPIError(401, "credentials required for this operation (no signer)")

    async def _request(
        self,
        method: str,
        endpoint: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any | None = None,
    ) -> dict[str, Any]:
        url = f"{self.base_url}{endpoint}"
        # Rate-limit reads only; writes (order placement/cancel) must never wait on a token.
        if self._read_limiter is not None and method.upper() == "GET":
            await self._read_limiter.acquire()
        resp = await request_with_retry(
            self._client,
            method,
            url,
            params=_clean(params),
            json=json,
            max_retries=self.max_retries,
            sign=self._sign,
        )
        if resp.status_code >= 400:
            raise KalshiAPIError(resp.status_code, resp.text)
        if not resp.content:
            return {}
        return resp.json()

    # -- market data ----------------------------------------------------- #
    async def get_events(
        self,
        *,
        series_ticker: str | None = None,
        status: str | None = None,
        limit: int = 200,
        cursor: str | None = None,
        with_nested_markets: bool = True,
    ) -> dict[str, Any]:
        return await self._request(
            "GET",
            "/events",
            params={
                "series_ticker": series_ticker,
                "status": status,
                "limit": limit,
                "cursor": cursor,
                "with_nested_markets": str(with_nested_markets).lower(),
            },
        )

    async def get_markets(
        self,
        *,
        series_ticker: str | None = None,
        event_ticker: str | None = None,
        status: str | None = None,
        tickers: str | None = None,
        limit: int = 200,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        return await self._request(
            "GET",
            "/markets",
            params={
                "series_ticker": series_ticker,
                "event_ticker": event_ticker,
                "status": status,
                "tickers": tickers,
                "limit": limit,
                "cursor": cursor,
            },
        )

    async def get_market(self, ticker: str) -> dict[str, Any]:
        return await self._request("GET", f"/markets/{ticker}")

    async def get_orderbook(self, ticker: str, *, depth: int = 10) -> dict[str, Any]:
        return await self._request("GET", f"/markets/{ticker}/orderbook", params={"depth": depth})

    # -- portfolio (auth) ------------------------------------------------ #
    async def get_balance(self) -> dict[str, Any]:
        self._require_auth()
        return await self._request("GET", "/portfolio/balance")

    async def get_positions(self, *, limit: int = 200, cursor: str | None = None) -> dict[str, Any]:
        self._require_auth()
        return await self._request(
            "GET", "/portfolio/positions", params={"limit": limit, "cursor": cursor}
        )

    async def get_fills(
        self,
        *,
        ticker: str | None = None,
        order_id: str | None = None,
        limit: int = 200,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """Trade fills for reconciliation: actual executed counts, prices, taker flag."""
        self._require_auth()
        return await self._request(
            "GET",
            "/portfolio/fills",
            params={"ticker": ticker, "order_id": order_id, "limit": limit, "cursor": cursor},
        )

    async def create_order(self, order: dict[str, Any]) -> dict[str, Any]:
        self._require_auth()
        return await self._request("POST", "/portfolio/orders", json=order)

    async def cancel_order(self, order_id: str) -> dict[str, Any]:
        self._require_auth()
        return await self._request("DELETE", f"/portfolio/orders/{order_id}")


def _clean(params: dict[str, Any] | None) -> dict[str, Any] | None:
    """Drop ``None`` values so we don't send empty query parameters."""
    if not params:
        return None
    return {k: v for k, v in params.items() if v is not None}
