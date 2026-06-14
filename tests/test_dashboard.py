"""Dashboard endpoints + kill switch."""

from fastapi.testclient import TestClient

from wc_kalshi.dashboard.app import create_app
from wc_kalshi.engine.builders import build_runtime


def _client(cfg, tmp_db):
    rt = build_runtime(cfg, db=tmp_db)
    return TestClient(create_app(rt)), rt


def test_index_and_health(cfg, tmp_db):
    client, _ = _client(cfg, tmp_db)
    assert client.get("/").status_code == 200
    assert client.get("/api/health").json()["ok"] is True


def test_state_and_calibration_serialize(cfg, tmp_db):
    client, _ = _client(cfg, tmp_db)
    # calibration is empty (NaN metrics) — must still serialize as valid JSON
    cal = client.get("/api/calibration")
    assert cal.status_code == 200
    assert "metrics" in cal.json()
    assert client.get("/api/state").status_code == 200


def test_kill_switch_endpoint_halts(cfg, tmp_db):
    client, rt = _client(cfg, tmp_db)
    assert rt.risk.trading_allowed
    resp = client.post("/api/kill").json()
    assert resp["kill_switch"] is True
    assert not rt.risk.trading_allowed
    assert client.get("/api/state").json()["risk"]["kill_switch"] is True
