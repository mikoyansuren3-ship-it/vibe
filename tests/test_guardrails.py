"""Always-on guardrails + kill switch."""

import pytest

from wc_kalshi.models.schemas import OrderAction
from wc_kalshi.risk.guardrails import RiskLimits, RiskManager


def _rm(**kw):
    limits = RiskLimits(
        max_position_per_market=kw.get("max_pos", 100),
        max_exposure_per_match=kw.get("max_match", 200.0),
        max_total_open_exposure=kw.get("max_total", 1000.0),
        max_daily_loss=kw.get("max_loss", 250.0),
        min_price=0.03,
        max_price=0.97,
        min_order_contracts=1,
    )
    return RiskManager(limits=limits)


def test_approves_within_limits():
    rm = _rm()
    d = rm.pre_trade_check(match_id="m1", market_ticker="t", action=OrderAction.BUY, contracts=10, cost_per_contract=0.5, price=0.5)
    assert d.approved and d.contracts == 10


def test_per_market_cap_clamps():
    rm = _rm(max_pos=100)
    d = rm.pre_trade_check(match_id="m1", market_ticker="t", action=OrderAction.BUY, contracts=150, cost_per_contract=0.5, price=0.5)
    assert d.approved and d.contracts == 100


def test_per_match_exposure_cap_clamps():
    rm = _rm(max_pos=10_000, max_match=50.0)
    d = rm.pre_trade_check(match_id="m1", market_ticker="t", action=OrderAction.BUY, contracts=1000, cost_per_contract=0.5, price=0.5)
    assert d.contracts == 100  # 50 dollars / 0.5


def test_total_exposure_cap_clamps():
    rm = _rm(max_pos=10_000, max_match=10_000, max_total=30.0)
    d = rm.pre_trade_check(match_id="m1", market_ticker="t", action=OrderAction.BUY, contracts=1000, cost_per_contract=0.5, price=0.5)
    assert d.contracts == 60  # 30 / 0.5


def test_price_band_rejects():
    rm = _rm()
    d = rm.pre_trade_check(match_id="m1", market_ticker="t", action=OrderAction.BUY, contracts=10, cost_per_contract=0.02, price=0.01)
    assert not d.approved


def test_daily_loss_halts_trading():
    rm = _rm(max_loss=250.0)
    rm.record_realized_pnl(-300.0)
    assert rm.halted and not rm.trading_allowed
    d = rm.pre_trade_check(match_id="m1", market_ticker="t", action=OrderAction.BUY, contracts=10, cost_per_contract=0.5, price=0.5)
    assert not d.approved and d.halted


def test_kill_switch_blocks_everything():
    rm = _rm()
    rm.engage_kill_switch("manual")
    assert rm.kill_switch_engaged and not rm.trading_allowed
    d = rm.pre_trade_check(match_id="m1", market_ticker="t", action=OrderAction.BUY, contracts=10, cost_per_contract=0.5, price=0.5)
    assert not d.approved


def test_register_fill_updates_position_and_exposure():
    rm = _rm()
    rm.register_fill(match_id="m1", market_ticker="t", action=OrderAction.BUY, contracts=10, cost_per_contract=0.5)
    assert rm.positions["t"] == 10
    assert rm.match_exposure["m1"] == 5.0
    # selling reduces the net position
    rm.register_fill(match_id="m1", market_ticker="t", action=OrderAction.SELL, contracts=4, cost_per_contract=0.5)
    assert rm.positions["t"] == 6


def test_closing_a_position_releases_exposure():
    rm = _rm()
    rm.register_fill(match_id="m1", market_ticker="t", action=OrderAction.BUY, contracts=100, cost_per_contract=0.5)
    assert rm.match_exposure["m1"] == pytest.approx(50.0)
    # partial close releases exposure pro-rata (the old add-only ledger grew to 75 here)
    rm.register_fill(match_id="m1", market_ticker="t", action=OrderAction.SELL, contracts=50, cost_per_contract=0.5)
    assert rm.positions["t"] == 50
    assert rm.match_exposure["m1"] == pytest.approx(25.0)
    # full close frees it entirely
    rm.register_fill(match_id="m1", market_ticker="t", action=OrderAction.SELL, contracts=50, cost_per_contract=0.5)
    assert rm.match_exposure.get("m1", 0.0) == 0.0
    assert rm.total_open_exposure == 0.0


def test_round_trip_churn_does_not_choke_new_trades():
    rm = _rm(max_match=60.0)
    rm.register_fill(match_id="m1", market_ticker="t", action=OrderAction.BUY, contracts=100, cost_per_contract=0.5)
    rm.register_fill(match_id="m1", market_ticker="t", action=OrderAction.SELL, contracts=100, cost_per_contract=0.5)
    # exposure is back to 0, so a fresh trade still fits under the per-match cap.
    # (The old add-only ledger would read $100 used and reject this.)
    d = rm.pre_trade_check(match_id="m1", market_ticker="t2", action=OrderAction.BUY, contracts=100, cost_per_contract=0.5, price=0.5)
    assert d.approved and d.contracts == 100


def test_settlement_clears_match_exposure():
    rm = _rm()
    rm.register_fill(match_id="m1", market_ticker="t", action=OrderAction.BUY, contracts=10, cost_per_contract=0.5)
    rm.record_realized_pnl(5.0, match_id="m1")
    assert rm.match_exposure.get("m1", 0.0) == 0.0
    assert rm.total_open_exposure == 0.0


def test_on_halt_callback_fires():
    fired = []
    rm = _rm(max_loss=10.0)
    rm.on_halt = lambda reason: fired.append(reason)
    rm.record_realized_pnl(-20.0)
    assert fired and "daily loss" in fired[0]
