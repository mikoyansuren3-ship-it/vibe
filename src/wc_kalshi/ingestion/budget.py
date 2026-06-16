"""Shared request budgeter (async token bucket).

The paid API-Football tier allows ~75,000 requests/day. With several World-Cup
group-stage matches live at once, each poll can fan out into many requests
(``/fixtures`` + per-fixture statistics/lineups/injuries), so an unbounded poll loop
could blow the daily quota in an afternoon. This token bucket is shared by every
football request: tokens refill at ``daily_budget / 86400`` per second with a small
burst, and ``acquire`` blocks until a token is available. Because it is one instance
shared across all concurrent matches, the *aggregate* request rate is bounded
regardless of how many matches are live.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

SECONDS_PER_DAY = 86_400


class RequestBudget:
    def __init__(
        self,
        daily_budget: int,
        *,
        burst: int | None = None,
        time_fn: Callable[[], float] = time.monotonic,
        sleep_fn: Callable[[float], "asyncio.Future"] | None = None,
    ) -> None:
        self.daily_budget = max(1, int(daily_budget))
        self.rate = self.daily_budget / SECONDS_PER_DAY  # tokens per second
        # Default burst = roughly one minute of steady-state requests (>=1), capped at
        # the daily budget so a tiny budget can't burst beyond itself.
        default_burst = max(1, round(self.rate * 60))
        self.capacity = float(min(self.daily_budget, burst or default_burst))
        self._tokens = self.capacity
        self._time = time_fn
        self._sleep = sleep_fn or asyncio.sleep
        self._last = self._time()
        self._lock = asyncio.Lock()
        self.granted = 0  # diagnostics: total tokens granted this process

    def _refill(self) -> None:
        now = self._time()
        elapsed = max(0.0, now - self._last)
        self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
        self._last = now

    async def acquire(self, n: int = 1) -> None:
        """Block until ``n`` tokens are available, then consume them."""
        n = max(1, n)
        async with self._lock:
            while True:
                self._refill()
                if self._tokens >= n:
                    self._tokens -= n
                    self.granted += n
                    return
                deficit = n - self._tokens
                await self._sleep(deficit / self.rate)

    def try_acquire(self, n: int = 1) -> bool:
        """Non-blocking variant: consume ``n`` tokens if available, else False."""
        n = max(1, n)
        self._refill()
        if self._tokens >= n:
            self._tokens -= n
            self.granted += n
            return True
        return False

    @property
    def available(self) -> float:
        self._refill()
        return self._tokens
