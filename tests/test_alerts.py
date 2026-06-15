"""Notification formatting + channel target building (offline, no network)."""

from wc_kalshi.eventbus import EventBus, EventType
from wc_kalshi.observability.alerts import Alerter, format_alert


def test_format_alert():
    assert "GOAL" in format_alert("goal", "Spain 1-0 Germany")
    assert format_alert("divergence", "x").startswith("📊")
    assert format_alert("guardrail", "daily loss hit").startswith("⛔")


def test_http_targets_for_enabled_channels(cfg):
    cfg = cfg.model_copy(deep=True)
    cfg.alerts.discord = True
    cfg.alerts.telegram = True
    cfg.alerts.webhook = False
    cfg.secrets.discord_webhook_url = "https://discord.test/wh"
    cfg.secrets.telegram_bot_token = "TOK"
    cfg.secrets.telegram_chat_id = "123"
    a = Alerter(cfg, EventBus())
    targets = a.http_targets("goal", "Spain 1-0", None, "⚽ GOAL — Spain 1-0")
    urls = [t["url"] for t in targets]
    assert any("discord.test" in u for u in urls)
    assert any("api.telegram.org/botTOK/sendMessage" in u for u in urls)
    disc = next(t for t in targets if "discord" in t["url"])
    assert disc["json"]["content"].startswith("⚽")
    tg = next(t for t in targets if "telegram" in t["url"])
    assert tg["json"]["chat_id"] == "123" and "Spain" in tg["json"]["text"]


def test_no_targets_when_channels_off(cfg):
    cfg = cfg.model_copy(deep=True)
    cfg.alerts.discord = cfg.alerts.telegram = cfg.alerts.webhook = False
    a = Alerter(cfg, EventBus())
    assert a.http_targets("goal", "x", None, "t") == []


def test_no_targets_when_secret_missing(cfg):
    cfg = cfg.model_copy(deep=True)
    cfg.alerts.discord = True
    cfg.secrets.discord_webhook_url = None  # enabled but no URL
    a = Alerter(cfg, EventBus())
    assert a.http_targets("goal", "x", None, "t") == []


def test_event_gating(cfg):
    cfg = cfg.model_copy(deep=True)
    cfg.alerts.on_goal = False
    cfg.alerts.on_fill = True
    a = Alerter(cfg, EventBus())
    assert a._should_send(EventType.ALERT, "goal") is False
    assert a._should_send(EventType.ALERT, "fill") is True
    assert a._should_send(EventType.ALERT, "divergence") is True  # default on
    cfg.alerts.on_guardrail = False
    assert a._should_send(EventType.GUARDRAIL, "guardrail") is False
