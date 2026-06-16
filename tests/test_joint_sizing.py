"""Joint 1X2 sizing: act on only the strongest leg per match per tick."""

from wc_kalshi.backtest.replay import Backtester


async def test_one_trade_per_tick_reduces_concentration(cfg):
    """Restricting to the single best leg per tick must not trade MORE than the old
    independent-per-leg behaviour, and must still trade something."""
    cfg_joint = cfg.model_copy(deep=True)
    cfg_joint.execution.one_trade_per_match_tick = True
    joint = await Backtester(cfg_joint, trade=True).run_synthetic(n_matches=30, seed0=0)

    cfg_indep = cfg.model_copy(deep=True)
    cfg_indep.execution.one_trade_per_match_tick = False
    indep = await Backtester(cfg_indep, trade=True).run_synthetic(n_matches=30, seed0=0)

    assert joint.n_fills <= indep.n_fills
    assert joint.n_fills > 0
