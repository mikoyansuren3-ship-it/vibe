"""Honest-evidence tooling: bootstrap CI, fixed-stake mode, CLV, informed market."""

from wc_kalshi.backtest.replay import Backtester, bootstrap_ci


def test_bootstrap_ci_brackets_mean():
    samples = [1.0, -0.5, 0.25, 2.0, -1.0, 0.75, 0.1, -0.2]
    lo, hi = bootstrap_ci(samples, iters=500, seed=1)
    mean = sum(samples) / len(samples)
    assert lo <= mean <= hi
    assert lo < hi


def test_bootstrap_ci_degenerate():
    assert bootstrap_ci([]) == (0.0, 0.0)
    assert bootstrap_ci([1.0]) == (0.0, 0.0)


async def test_fixed_stake_mode_reports_and_runs(cfg):
    bt = Backtester(cfg, trade=True, stake_mode="fixed", fixed_stake=20.0)
    res = await bt.run_synthetic(n_matches=10, seed0=0)
    assert res.stake_mode == "fixed"
    assert res.pnl_ci[0] <= res.pnl_ci[1]
    assert "stake mode:         fixed" in res.report()
    await bt.aclose()


async def test_clv_recorded_when_trading(cfg):
    bt = Backtester(cfg, trade=True)
    res = await bt.run_synthetic(n_matches=12, seed0=0)
    # With trades, some fills should have a usable closing mid for CLV.
    assert res.clv_n > 0
    await bt.aclose()


async def test_market_awareness_shrinks_edge(cfg):
    """The honest de-circularisation result: a book that prices like our model leaves
    us (almost) no edge, so the number of fills collapses as awareness rises."""
    cfg_blind = cfg.model_copy(deep=True)
    cfg_blind.football.sim_market_xg_awareness = 0.0
    blind = await Backtester(cfg_blind, trade=True).run_synthetic(n_matches=40, seed0=0)

    cfg_sharp = cfg.model_copy(deep=True)
    cfg_sharp.football.sim_market_xg_awareness = 1.0
    sharp = await Backtester(cfg_sharp, trade=True).run_synthetic(n_matches=40, seed0=0)

    assert sharp.n_fills < blind.n_fills
