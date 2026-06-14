"""Fractional-Kelly sizing."""

from wc_kalshi.models.schemas import EdgeSignal, OrderAction, Outcome
from wc_kalshi.risk.sizing import PositionSizer, kelly_fraction_for_trade


def _edge(action, model_prob, ask=None, bid=None):
    return EdgeSignal(
        match_id="m1",
        outcome=Outcome.HOME,
        market_ticker="T-home",
        model_prob=model_prob,
        market_prob=0.5,
        market_yes_ask=ask,
        market_yes_bid=bid,
        raw_edge=0.1,
        est_cost=0.01,
        net_edge=0.08,
        action=action,
        actionable=True,
    )


def test_kelly_formula_buy():
    f, cost, win = kelly_fraction_for_trade(0.60, 0.50, OrderAction.BUY)
    assert abs(f - 0.20) < 1e-9  # (q-p)/(1-p)
    assert cost == 0.50
    assert win == 0.60


def test_kelly_formula_sell():
    f, cost, win = kelly_fraction_for_trade(0.40, 0.50, OrderAction.SELL)
    assert abs(f - 0.20) < 1e-9  # (p-q)/p
    assert cost == 0.50
    assert abs(win - 0.60) < 1e-9


def test_no_edge_sizes_zero():
    f, _, _ = kelly_fraction_for_trade(0.50, 0.50, OrderAction.BUY)
    assert f == 0.0


def test_sizer_produces_capped_contracts():
    sizer = PositionSizer(kelly_fraction=0.25, max_position_per_market=100, max_exposure_per_match=200)
    d = sizer.size(_edge(OrderAction.BUY, 0.60, ask=50), bankroll=1000)
    assert d.is_trade
    assert 0 < d.contracts <= 100
    assert d.exposure_dollars <= 200 + 1e-9


def test_calibration_factor_scales_down():
    sizer = PositionSizer(kelly_fraction=0.25, max_position_per_market=10_000, max_exposure_per_match=10_000)
    full = sizer.size(_edge(OrderAction.BUY, 0.60, ask=50), bankroll=1000, calibration_factor=1.0)
    half = sizer.size(_edge(OrderAction.BUY, 0.60, ask=50), bankroll=1000, calibration_factor=0.5)
    assert half.contracts < full.contracts


def test_per_market_room_respected():
    sizer = PositionSizer(kelly_fraction=0.25, max_position_per_market=100, max_exposure_per_match=10_000)
    d = sizer.size(_edge(OrderAction.BUY, 0.60, ask=50), bankroll=1000, existing_contracts=95)
    assert d.contracts <= 5


def test_not_actionable_no_trade():
    sizer = PositionSizer()
    e = _edge(OrderAction.BUY, 0.60, ask=50)
    e.actionable = False
    assert not sizer.size(e, bankroll=1000).is_trade
