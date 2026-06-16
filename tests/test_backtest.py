"""Backtest harness: runs offline, populates metrics, deterministic."""

from wc_kalshi.backtest.replay import Backtester
from wc_kalshi.engine.match_loop import CALIBRATION_CHECKPOINTS


async def test_backtest_runs_and_reports(cfg):
    bt = Backtester(cfg, trade=True)
    res = await bt.run_synthetic(n_matches=12, seed0=0)
    assert res.n_matches == 12
    # Calibration now pools in-play predictions across the match (checkpoints) plus
    # every traded prediction — many points per match, not just the minute-1 prior.
    assert res.calibration["n"] >= len(CALIBRATION_CHECKPOINTS) * 12
    assert res.starting_bankroll == cfg.risk.starting_bankroll
    assert "BACKTEST REPORT" in res.report()
    await bt.aclose()


async def test_backtest_is_deterministic(cfg):
    bt1 = Backtester(cfg, trade=True)
    r1 = await bt1.run_synthetic(n_matches=8, seed0=100)
    await bt1.aclose()
    bt2 = Backtester(cfg, trade=True)
    r2 = await bt2.run_synthetic(n_matches=8, seed0=100)
    await bt2.aclose()
    assert abs(r1.realized_pnl - r2.realized_pnl) < 1e-6
    assert r1.n_fills == r2.n_fills


async def test_no_trade_mode_places_nothing(cfg):
    bt = Backtester(cfg, trade=False)
    res = await bt.run_synthetic(n_matches=10, seed0=3)
    assert res.n_fills == 0
    assert res.fees_paid == 0.0
    # No trades -> exactly the checkpoint predictions per settled match accrue.
    assert res.calibration["n"] == len(CALIBRATION_CHECKPOINTS) * 10
    await bt.aclose()
