"""Shared async HTTP retry helper (tenacity), honouring ``Retry-After``.

All outbound REST calls (Kalshi + football providers) go through
``request_with_retry`` so back-off, jitter, and 429 handling are consistent and
testable in one place.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

# A server can name any Retry-After it likes; an exhausted-quota response with a value in
# the hundreds of seconds would otherwise sleep the whole live loop with positions open. We
# honour the hint but never freeze longer than this — the caller (orchestrator) also wraps
# the poll in a hard timeout as a second line of defence.
RETRY_AFTER_CAP_SECONDS = 60.0


class RateLimited(Exception):
    """Raised on HTTP 429 so the retry layer can honour Retry-After."""

    def __init__(self, retry_after: float | None) -> None:
        super().__init__("rate limited")
        self.retry_after = retry_after


class ServerError(Exception):
    """Raised on 5xx so the retry layer treats it as transient."""

    def __init__(self, status: int) -> None:
        super().__init__(f"server error {status}")
        self.status = status


def _parse_retry_after(resp: httpx.Response) -> float | None:
    value = resp.headers.get("Retry-After")
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _wait(state: Any) -> float:
    """Exponential jitter back-off, but honour an explicit Retry-After if present —
    clamped so a hostile/huge hint can't freeze the loop (see RETRY_AFTER_CAP_SECONDS)."""
    exc = state.outcome.exception() if state.outcome else None
    if isinstance(exc, RateLimited) and exc.retry_after is not None:
        return max(0.0, min(exc.retry_after, RETRY_AFTER_CAP_SECONDS))
    return wait_exponential_jitter(initial=0.5, max=30.0)(state)


async def request_with_retry(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    json: Any | None = None,
    max_retries: int = 4,
    sign: Any | None = None,
    on_attempt: Callable[[], Awaitable[None]] | None = None,
) -> httpx.Response:
    """Issue a request with retry/back-off.

    ``sign`` (optional) is a callable ``(method, url) -> dict[str, str]`` returning
    fresh auth headers; it is invoked on *every* attempt so time-based signatures
    are never stale on a retry.

    ``on_attempt`` (optional) is awaited once *per HTTP attempt* — pass a request-budget
    ``acquire`` here so a retried request spends a token for every wire request it makes,
    not just one per logical call (otherwise a 3-retry call under-accounts quota by 3×).
    """
    async for attempt in AsyncRetrying(
        stop=stop_after_attempt(max(1, max_retries)),
        wait=_wait,
        retry=retry_if_exception_type((RateLimited, ServerError, httpx.TransportError)),
        reraise=True,
    ):
        with attempt:
            if on_attempt is not None:
                await on_attempt()  # one budget token for THIS wire request (retries included)
            req_headers = dict(headers or {})
            if sign is not None:
                req_headers.update(sign(method, url))
            resp = await client.request(
                method, url, headers=req_headers, params=params, json=json
            )
            if resp.status_code == 429:
                raise RateLimited(_parse_retry_after(resp))
            if resp.status_code >= 500:
                raise ServerError(resp.status_code)
            return resp
    raise RuntimeError("unreachable: AsyncRetrying exhausted without raising")
