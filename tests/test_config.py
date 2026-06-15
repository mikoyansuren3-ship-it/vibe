"""Config loading + the safety-critical live-mode gate."""

import pytest

from wc_kalshi.config import REPO_ROOT, ConfigError, RunMode, load_config


def test_advisory_and_auto_config_files(monkeypatch):
    monkeypatch.setenv("WCK_CONFIG", str(REPO_ROOT / "config" / "advisory.yaml"))
    adv = load_config(load_env=False)
    assert adv.execution.decision_mode == "advisory"
    assert adv.dashboard.port == 8000

    monkeypatch.setenv("WCK_CONFIG", str(REPO_ROOT / "config" / "auto.yaml"))
    auto = load_config(load_env=False)
    assert auto.execution.decision_mode == "autonomous"
    assert auto.dashboard.port == 8001
    assert auto.risk.kelly_fraction == 0.15  # tighter than advisory


def test_paper_is_default_and_boots():
    cfg = load_config(load_env=False)
    assert cfg.mode is RunMode.PAPER
    assert cfg.is_paper
    assert "demo" in cfg.kalshi_rest_base  # paper points at demo URLs, never prod


def test_resolved_db_url_absolutizes(tmp_path, monkeypatch):
    cfg = load_config(load_env=False)
    url = cfg.resolved_db_url()
    assert url.startswith("sqlite:////")  # absolute path => four slashes


def test_live_mode_blocked_without_gates(monkeypatch):
    monkeypatch.setenv("WCK_MODE", "live")
    monkeypatch.delenv("WCK_ALLOW_LIVE", raising=False)
    with pytest.raises(ConfigError) as exc:
        load_config(load_env=False)
    assert "LIVE" in str(exc.value)


def test_live_mode_blocked_with_partial_gates(monkeypatch):
    # allow_live env set, but execution.live_confirmed stays False in default yaml
    monkeypatch.setenv("WCK_MODE", "live")
    monkeypatch.setenv("WCK_ALLOW_LIVE", "true")
    with pytest.raises(ConfigError):
        load_config(load_env=False)


def test_demo_mode_allowed(monkeypatch):
    monkeypatch.setenv("WCK_MODE", "demo")
    cfg = load_config(load_env=False)
    assert cfg.is_demo
