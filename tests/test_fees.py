"""Kalshi fee formula (research.md §1.6)."""

from wc_kalshi.fees import fee_per_contract, kalshi_fee


def test_taker_fee_at_fifty_cents():
    # ceil(0.07 * 1 * 0.5 * 0.5 * 100)/100 = ceil(1.75)/100 = 0.02
    assert kalshi_fee(1, 0.50) == 0.02


def test_maker_is_about_a_quarter():
    # raw 0.004375 -> ceil(0.4375)=1 cent
    assert kalshi_fee(1, 0.50, maker=True) == 0.01


def test_fee_peaks_at_fifty():
    mid = fee_per_contract(0.50)
    assert fee_per_contract(0.10) < mid
    assert fee_per_contract(0.90) < mid
    assert abs(mid - 0.0175) < 1e-9


def test_fee_zero_for_no_contracts():
    assert kalshi_fee(0, 0.5) == 0.0


def test_fee_scales_with_contracts():
    assert kalshi_fee(100, 0.50) >= kalshi_fee(10, 0.50)
    # 100 contracts: ceil(0.07*100*0.25*100)/100 = ceil(175)/100 = 1.75
    assert kalshi_fee(100, 0.50) == 1.75
