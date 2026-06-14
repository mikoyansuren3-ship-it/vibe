"""Kalshi fee model (research.md §1.6).

Per-order taker fee (rounded UP to the next whole cent on the order total):

    fee = ceil( coefficient * contracts * price * (1 - price) * 100 ) / 100   [USD]

Maker fee is roughly a quarter of taker. Coefficient defaults to the general 0.07
but is configurable for series with a bespoke schedule. Because a Yes contract
settles at $1.00, a per-contract fee in dollars equals a cost in probability units,
which is how the edge detector subtracts it.
"""

from __future__ import annotations

import math

from .util import clamp


def kalshi_fee(
    contracts: int,
    price: float,
    *,
    coefficient: float = 0.07,
    maker: bool = False,
    maker_fraction: float = 0.25,
) -> float:
    """Return the trading fee in dollars for an order of ``contracts`` at ``price``."""
    if contracts <= 0:
        return 0.0
    price = clamp(price, 0.0, 1.0)
    raw = coefficient * contracts * price * (1.0 - price)
    if maker:
        raw *= maker_fraction
    # round() before ceil() to kill floating-point noise (e.g. 1.7500000002 -> 1.75)
    # that would otherwise spuriously round a fee up by a whole cent.
    return math.ceil(round(raw * 100, 9)) / 100.0


def fee_per_contract(
    price: float, *, coefficient: float = 0.07, maker: bool = False, maker_fraction: float = 0.25
) -> float:
    """Approximate per-contract fee (un-rounded) in dollars / probability units."""
    price = clamp(price, 0.0, 1.0)
    raw = coefficient * price * (1.0 - price)
    if maker:
        raw *= maker_fraction
    return raw
