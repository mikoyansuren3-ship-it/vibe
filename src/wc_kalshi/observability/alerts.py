"""Alerter: subscribes to the event bus and delivers priority alerts.

Priority events (goal, red card, model/market divergence, guardrail trip) are
logged to the console and optionally POSTed to a webhook. Delivery is best-effort
and never blocks the trading loop.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import httpx

from ..eventbus import Event, EventBus, EventType
from ..logging_setup import get_logger

if TYPE_CHECKING:
    from ..config import AppConfig

log = get_logger("alerts")


class Alerter:
    def __init__(self, cfg: "AppConfig", bus: EventBus) -> None:
        self.cfg = cfg
        self.bus = bus
        self.webhook_url = cfg.secrets.alerts_webhook_url
        self._client: httpx.AsyncClient | None = None

    def start(self) -> None:
        if self.cfg.alerts.enabled:
            self.bus.subscribe(self._on_event)

    def _on_event(self, event: Event) -> None:
        if event.type not in {EventType.ALERT, EventType.GUARDRAIL}:
            return
        if event.type is EventType.GUARDRAIL and not self.cfg.alerts.on_guardrail:
            return
        kind = event.payload.get("kind", event.type.value)
        message = event.payload.get("message") or event.payload.get("reason", "")
        if event.type is EventType.ALERT:
            if kind == "goal" and not self.cfg.alerts.on_goal:
                return
            if kind == "red_card" and not self.cfg.alerts.on_red_card:
                return
        if self.cfg.alerts.console:
            level = log.warning if event.type is EventType.GUARDRAIL else log.info
            level(f"ALERT[{kind}] {message}", extra={"match_id": event.match_id, "kind": kind})
        if self.cfg.alerts.webhook and self.webhook_url:
            self._dispatch_webhook(kind, message, event.match_id)

    def _dispatch_webhook(self, kind: str, message: str, match_id: str | None) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no loop; skip (e.g., in a sync test)
        loop.create_task(self._post({"kind": kind, "message": message, "match_id": match_id}))

    async def _post(self, payload: dict) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=5.0)
        try:
            await self._client.post(self.webhook_url, json=payload)
        except Exception as exc:
            log.warning("webhook post failed", extra={"err": str(exc)})

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
