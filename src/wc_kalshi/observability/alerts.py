"""Alerter: subscribes to the event bus and delivers priority alerts.

Priority events (goal, red card, divergence, new proposal, fill, guardrail trip) are
logged to the console and optionally pushed to a generic webhook, **Discord**,
**Telegram**, and/or **email**. Delivery is best-effort, opt-in per channel, and never
blocks the trading loop. Message formatting + target building are pure functions so
they can be unit-tested offline.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import httpx

from ..eventbus import Event, EventBus, EventType
from ..logging_setup import get_logger

if TYPE_CHECKING:
    from ..config import AppConfig

log = get_logger("alerts")

_PREFIX = {
    "goal": "⚽ GOAL",
    "red_card": "🟥 RED CARD",
    "divergence": "📊 Divergence",
    "guardrail": "⛔ Guardrail",
    "proposal": "⚡ New proposal",
    "fill": "✅ Fill",
}


def format_alert(kind: str, message: str) -> str:
    """Human-readable one-line alert text (pure, testable)."""
    return f"{_PREFIX.get(kind, kind.upper())} — {message}".strip(" —")


class Alerter:
    def __init__(self, cfg: "AppConfig", bus: EventBus) -> None:
        self.cfg = cfg
        self.bus = bus
        self.s = cfg.secrets
        self._client: httpx.AsyncClient | None = None

    def start(self) -> None:
        if self.cfg.alerts.enabled:
            self.bus.subscribe(self._on_event)

    # -- gating ---------------------------------------------------------- #
    def _should_send(self, event_type: EventType, kind: str) -> bool:
        a = self.cfg.alerts
        if event_type is EventType.GUARDRAIL:
            return a.on_guardrail
        gates = {
            "goal": a.on_goal,
            "red_card": a.on_red_card,
            "proposal": a.on_proposal,
            "fill": a.on_fill,
        }
        return gates.get(kind, True)  # divergence + unknown kinds default on

    def _on_event(self, event: Event) -> None:
        if event.type not in {EventType.ALERT, EventType.GUARDRAIL}:
            return
        kind = event.payload.get("kind", "guardrail" if event.type is EventType.GUARDRAIL else "alert")
        message = event.payload.get("message") or event.payload.get("reason", "")
        if not self._should_send(event.type, kind):
            return
        text = format_alert(kind, message)
        if self.cfg.alerts.console:
            (log.warning if event.type is EventType.GUARDRAIL else log.info)(
                f"ALERT[{kind}] {message}", extra={"match_id": event.match_id, "kind": kind}
            )
        self._dispatch(kind, message, event.match_id, text)

    # -- target building (pure, testable) -------------------------------- #
    def http_targets(self, kind: str, message: str, match_id: str | None, text: str) -> list[dict[str, Any]]:
        """Return the list of {url, json} HTTP posts for enabled channels."""
        a, s = self.cfg.alerts, self.s
        out: list[dict[str, Any]] = []
        if a.webhook and s.alerts_webhook_url:
            out.append({"url": s.alerts_webhook_url, "json": {"kind": kind, "message": message, "match_id": match_id}})
        if a.discord and s.discord_webhook_url:
            out.append({"url": s.discord_webhook_url, "json": {"content": text}})
        if a.telegram and s.telegram_bot_token and s.telegram_chat_id:
            out.append({
                "url": f"https://api.telegram.org/bot{s.telegram_bot_token}/sendMessage",
                "json": {"chat_id": s.telegram_chat_id, "text": text},
            })
        return out

    # -- dispatch -------------------------------------------------------- #
    def _dispatch(self, kind: str, message: str, match_id: str | None, text: str) -> None:
        targets = self.http_targets(kind, message, match_id, text)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no loop (e.g. sync test) — skip network
        for t in targets:
            loop.create_task(self._post(t["url"], t["json"]))
        if self.cfg.alerts.email and self.s.smtp_host and self.s.email_to:
            loop.create_task(asyncio.to_thread(self._send_email, text))

    async def _post(self, url: str, payload: dict) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=5.0)
        try:
            await self._client.post(url, json=payload)
        except Exception as exc:
            log.warning("alert post failed", extra={"err": str(exc)})

    def _send_email(self, text: str) -> None:
        import smtplib
        from email.message import EmailMessage

        s = self.s
        try:
            msg = EmailMessage()
            msg["Subject"] = "[WCK] alert"
            msg["From"] = s.email_from or s.smtp_user or "wck@localhost"
            msg["To"] = s.email_to
            msg.set_content(text)
            with smtplib.SMTP(s.smtp_host, s.smtp_port or 587, timeout=8) as server:
                server.starttls()
                if s.smtp_user:
                    server.login(s.smtp_user, s.smtp_password or "")
                server.send_message(msg)
        except Exception as exc:
            log.warning("alert email failed", extra={"err": str(exc)})

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
