"""Metric-namespace walls: CLV/edge from real Kalshi quotes, a synthetic market,
StatsBomb calibration (no prices), and Betfair lines are different measurements and must
never be pooled. Every result is tagged; combining across tags raises."""

import pytest

from wc_kalshi.backtest.replay import BacktestResult, DataSource, require_same_source


def test_require_same_source_allows_uniform():
    a = BacktestResult(data_source=DataSource.KALSHI_REPLAY)
    b = BacktestResult(data_source=DataSource.KALSHI_REPLAY)
    assert require_same_source(a, b) == DataSource.KALSHI_REPLAY


def test_require_same_source_rejects_conflation():
    real = BacktestResult(data_source=DataSource.KALSHI_REPLAY)
    synthetic = BacktestResult(data_source=DataSource.SYNTHETIC)
    with pytest.raises(ValueError, match="across data sources"):
        require_same_source(real, synthetic)


def test_to_dict_carries_data_source():
    r = BacktestResult(data_source=DataSource.STATSBOMB_CALIBRATION)
    assert r.to_dict()["data_source"] == "statsbomb_calibration"


def test_all_four_namespaces_are_distinct():
    assert len(DataSource.ALL) == 4
