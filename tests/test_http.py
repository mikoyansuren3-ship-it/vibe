"""Shared async HTTP retry helper: Retry-After clamp + per-attempt budget accounting."""

import httpx

from wc_kalshi.ingestion.http import (
    RETRY_AFTER_CAP_SECONDS,
    RateLimited,
    _wait,
    request_with_retry,
)


class _State:
    """Minimal stand-in for a tenacity RetryCallState carrying a failed outcome."""

    def __init__(self, exc):
        class _Outcome:
            def exception(self):
                return exc

        self.outcome = _Outcome()


def test_wait_clamps_retry_after_to_the_cap():
    # A huge/hostile hint is clamped so it can't freeze the loop with positions open.
    assert _wait(_State(RateLimited(retry_after=600.0))) == RETRY_AFTER_CAP_SECONDS
    # A hint under the cap is honoured verbatim.
    assert _wait(_State(RateLimited(retry_after=5.0))) == 5.0
    # A negative/zero hint can never produce a negative sleep.
    assert _wait(_State(RateLimited(retry_after=-3.0))) == 0.0


async def test_on_attempt_fires_once_per_wire_attempt(monkeypatch):
    """A retried request must invoke on_attempt for EVERY wire attempt, so a budget token
    is spent per real request — not once per logical call (which under-accounts on retry)."""
    attempts = {"acquired": 0, "requested": 0}

    async def on_attempt():
        attempts["acquired"] += 1

    # 429 (Retry-After 0 → no real sleep) then 200: exactly two wire attempts.
    responses = [
        httpx.Response(429, headers={"Retry-After": "0"}),
        httpx.Response(200, json={}),
    ]

    async def fake_request(method, url, **kwargs):
        attempts["requested"] += 1
        return responses.pop(0)

    client = httpx.AsyncClient()
    monkeypatch.setattr(client, "request", fake_request)
    try:
        resp = await request_with_retry(
            client, "GET", "https://x/y", on_attempt=on_attempt, max_retries=4
        )
        assert resp.status_code == 200
        assert attempts["requested"] == 2  # one 429 + one 200
        assert attempts["acquired"] == 2  # a token for each wire attempt, not just one
    finally:
        await client.aclose()
