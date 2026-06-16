"""Real-data historical backtest loader + run."""

import json
from pathlib import Path

from wc_kalshi.backtest.historical import (
    has_market_data,
    load_historical_file,
    load_historical_match,
)
from wc_kalshi.backtest.replay import Backtester
from wc_kalshi.models.schemas import MatchPeriod, Outcome

DATA = Path(__file__).parent / "data" / "historical_match.json"


def test_loader_maps_ticks_and_markets():
    matches = load_historical_file(DATA)
    assert len(matches) == 1
    ticks = matches[0]
    assert len(ticks) == 6
    first_snap, first_mk = ticks[0]
    assert first_snap.home_team == "Atlantis"
    assert first_snap.context.home_elo == 1980
    assert len(first_mk) == 3  # home/draw/away quotes
    # final tick is full-time and has no market quotes
    last_snap, last_mk = ticks[-1]
    assert last_snap.period is MatchPeriod.FULL_TIME
    assert last_mk == []
    assert has_market_data(matches)


async def test_run_historical_with_prices(cfg):
    matches = load_historical_file(DATA)
    bt = Backtester(cfg, trade=True)
    res = await bt.run_historical(matches)
    assert res.n_matches == 1
    # one settled match -> calibration accrued from real outcome
    assert res.calibration["n"] >= 1
    await bt.aclose()


async def test_xg_only_mode_degrades_gracefully(cfg, tmp_path):
    """No market prices anywhere -> still scores model calibration, no trades."""
    raw = json.loads(DATA.read_text())
    for t in raw["ticks"]:
        t.pop("markets", None)
    p = tmp_path / "xg_only.json"
    p.write_text(json.dumps(raw))

    matches = load_historical_file(p)
    assert not has_market_data(matches)
    bt = Backtester(cfg, trade=True)
    res = await bt.run_historical(matches)
    assert res.n_fills == 0  # nothing to trade without prices
    assert res.calibration["n"] >= 1  # but calibration still measured
    await bt.aclose()


def test_realized_outcome_matches_final_score():
    ticks = load_historical_match(json.loads(DATA.read_text()))
    final = ticks[-1][0]
    assert final.home_score > final.away_score  # home won -> HOME outcome
    assert final.score_diff == 2
    _ = Outcome.HOME
