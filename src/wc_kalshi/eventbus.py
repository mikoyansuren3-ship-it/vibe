"""A tiny async pub/sub event bus.

Each match loop runs as its own asyncio task and publishes events here; alerts,
the dashboard, and the audit logger subscribe. Decoupling the producers from the
observers keeps the trading loop fast and side-effect-free.

Supports two subscription styles:
  * ``subscribe(callback)``    — synchronous hook called inline on publish.
  * ``stream()``               — async iterator (backed by a queue) for consumers
                                 like the dashboard's server-sent-events feed.
"""

from __future__ import annotations

import asyncio
import enum
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

from .util import utcnow


class EventType(str, enum.Enum):
    MATCH_SNAPSHOT = "match_snapshot"
    MARKET_SNAPSHOT = "market_snapshot"
    PROBABILITIES = "probabilities"
    EDGE = "edge"
    ORDER = "order"
    FILL = "fill"
    ALERT = "alert"
    GUARDRAIL = "guardrail"
    KILL_SWITCH = "kill_switch"
    PNL = "pnl"


@dataclass
class Event:
    type: EventType
    payload: dict[str, Any] = field(default_factory=dict)
    match_id: str | None = None
    ts: str = field(default_factory=lambda: utcnow().isoformat())


class EventBus:
    def __init__(self, queue_maxsize: int = 1000) -> None:
        self._callbacks: list[Callable[[Event], None]] = []
        self._queues: list[asyncio.Queue[Event]] = []
        self._queue_maxsize = queue_maxsize

    def subscribe(self, callback: Callable[[Event], None]) -> None:
        """Register a synchronous callback invoked inline on every publish."""
        self._callbacks.append(callback)

    def publish(self, event: Event) -> None:
        for cb in self._callbacks:
            try:
                cb(event)
            except Exception:  # never let an observer break the trading loop
                pass
        for q in self._queues:
            if q.full():
                # Drop oldest to keep producers non-blocking.
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            q.put_nowait(event)

    async def stream(self) -> AsyncIterator[Event]:
        """Yield events as they are published (for SSE / live consumers)."""
        q: asyncio.Queue[Event] = asyncio.Queue(maxsize=self._queue_maxsize)
        self._queues.append(q)
        try:
            while True:
                yield await q.get()
        finally:
            self._queues.remove(q)
