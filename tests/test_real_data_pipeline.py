"""End-to-end: StatsBomb (+ optional Betfair) -> run_historical metrics. Offline."""

import json
from pathlib import Path

from wc_kalshi.backtest.betfair import merge_markets, parse_stream_path
from wc_kalshi.backtest.historical import has_market_data, load_historical_match
from wc_kalshi.backtest.replay import Backtester
from wc_kalshi.backtest.statsbomb import convert_match

DATA = Path(__file__).parent / "data"
META = json.loads((DATA / "statsbomb_match_meta.json").read_text())
EVENTS = json.loads((DATA / "statsbomb_events_sample.json").read_text())


async def test_xg_only_scores_calibration_no_clv(cfg):
    """No market prices -> real calibration on the real outcome, but no fills / no CLV."""
    match = convert_match(META, EVENTS)
    ticks = load_historical_match(match)
    assert not has_market_data([ticks])
    bt = Backtester(cfg, trade=True, stake_mode="fixed")
    res = await bt.run_historical([ticks])
    assert res.calibration["n"] >= 1
    assert res.n_fills == 0
    assert res.clv_n == 0 and res.clv_n_preoff == 0
    await bt.aclose()


async def test_betfair_merge_enables_clv(cfg):
    """With merged Betfair quotes the dataset carries prices, so CLV is measurable and the
    pre-off reference exists for every fill (it sits on the earliest tick)."""
    match = convert_match(META, EVENTS)
    merge_markets([match], parse_stream_path(DATA / "betfair_stream_sample.ndjson"))
    ticks = load_historical_match(match)
    assert has_market_data([ticks])
    bt = Backtester(cfg, trade=True, stake_mode="fixed")
    res = await bt.run_historical([ticks])
    assert res.calibration["n"] >= 1
    assert bt._mid_history  # mids captured for the reference lines
    # Every fill has a pre-off reference (pre-off quote is on tick 0).
    assert res.clv_n_preoff == res.clv_n
    await bt.aclose()
