"""Portfolio accounting + settlement."""

from wc_kalshi.execution.portfolio import Portfolio
from wc_kalshi.models.schemas import OrderAction, Outcome


def test_buy_yes_settles_win():
    p = Portfolio(starting_bankroll=1000.0)
    p.apply_fill(match_id="m1", market_ticker="t", outcome=Outcome.HOME, action=OrderAction.BUY, contracts=100, price_cents=40, fee=0.0)
    assert p.cash == 1000.0 - 40.0  # paid 0.40 * 100
    pnl = p.settle_market("t", yes_won=True)  # pays $1 each
    assert pnl == 100 - 40
    assert p.realized_pnl == 60.0
    assert p.cash == 1000.0 - 40.0 + 100.0


def test_buy_yes_settles_loss():
    p = Portfolio(starting_bankroll=1000.0)
    p.apply_fill(match_id="m1", market_ticker="t", outcome=Outcome.HOME, action=OrderAction.BUY, contracts=100, price_cents=40, fee=0.0)
    pnl = p.settle_market("t", yes_won=False)
    assert pnl == -40.0


def test_sell_yes_buys_no_and_settles():
    p = Portfolio(starting_bankroll=1000.0)
    # SELL yes at 60c == buy No at 40c
    p.apply_fill(match_id="m1", market_ticker="t", outcome=Outcome.HOME, action=OrderAction.SELL, contracts=100, price_cents=60, fee=0.0)
    assert abs(p.cash - (1000.0 - 40.0)) < 1e-9
    pnl = p.settle_market("t", yes_won=False)  # No wins
    assert abs(pnl - (100 - 40)) < 1e-9


def test_settle_match_by_outcome():
    p = Portfolio(starting_bankroll=1000.0)
    p.apply_fill(match_id="m1", market_ticker="home", outcome=Outcome.HOME, action=OrderAction.BUY, contracts=50, price_cents=50, fee=0.0)
    p.apply_fill(match_id="m1", market_ticker="away", outcome=Outcome.AWAY, action=OrderAction.BUY, contracts=50, price_cents=30, fee=0.0)
    p.settle_match("m1", Outcome.HOME)
    # home paid out, away lost; both markets cleared
    assert "home" not in p.positions and "away" not in p.positions


def test_unrealized_marks_to_mid():
    p = Portfolio(starting_bankroll=1000.0)
    p.apply_fill(match_id="m1", market_ticker="t", outcome=Outcome.HOME, action=OrderAction.BUY, contracts=100, price_cents=40, fee=0.0)
    # mid now 0.60 => holdings worth 60, cost 40 => +20 unrealized
    assert abs(p.unrealized_pnl({"t": 0.60}) - 20.0) < 1e-9


def test_bankroll_tracks_realized_only():
    p = Portfolio(starting_bankroll=1000.0)
    p.apply_fill(match_id="m1", market_ticker="t", outcome=Outcome.HOME, action=OrderAction.BUY, contracts=100, price_cents=40, fee=0.0)
    p.settle_market("t", yes_won=True)
    assert p.bankroll() == 1000.0 + 60.0


def test_fees_accumulate():
    p = Portfolio(starting_bankroll=1000.0)
    p.apply_fill(match_id="m1", market_ticker="t", outcome=Outcome.HOME, action=OrderAction.BUY, contracts=10, price_cents=50, fee=0.2)
    assert p.fees_paid == 0.2
    assert abs(p.cash - (1000.0 - 5.0 - 0.2)) < 1e-9


def test_offsetting_yes_no_lots_net_to_cash():
    p = Portfolio(starting_bankroll=1000.0)
    # Buy 100 Yes @ 40c, then SELL 100 Yes @ 60c (== buy 100 No @ 40c).
    p.apply_fill(match_id="m1", market_ticker="t", outcome=Outcome.HOME, action=OrderAction.BUY, contracts=100, price_cents=40, fee=0.0)
    p.apply_fill(match_id="m1", market_ticker="t", outcome=Outcome.HOME, action=OrderAction.SELL, contracts=100, price_cents=60, fee=0.0)
    # Matched 100 pairs realize $100; cost was 40 + 40 = 80 => +20 realized, flat position.
    assert "t" not in p.positions  # fully netted and removed
    assert abs(p.realized_pnl - 20.0) < 1e-9
    # cash: 1000 - 40 - 40 + 100 = 1020
    assert abs(p.cash - 1020.0) < 1e-9


def test_partial_netting_keeps_residual():
    p = Portfolio(starting_bankroll=1000.0)
    p.apply_fill(match_id="m1", market_ticker="t", outcome=Outcome.HOME, action=OrderAction.BUY, contracts=100, price_cents=50, fee=0.0)
    p.apply_fill(match_id="m1", market_ticker="t", outcome=Outcome.HOME, action=OrderAction.SELL, contracts=30, price_cents=50, fee=0.0)
    # 30 pairs net out, 70 Yes remain.
    assert p.positions["t"].net_yes == 70
    assert p.positions["t"].yes_contracts == 70 and p.positions["t"].no_contracts == 0
