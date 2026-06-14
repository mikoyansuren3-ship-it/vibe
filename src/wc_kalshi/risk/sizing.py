"""Position sizing via fractional Kelly (never full Kelly).

For a Kalshi Yes contract bought at price ``p`` (cost ``p``, pays $1) with model
win-probability ``q``, the full-Kelly fraction of bankroll to put *at risk* is::

    f* = (q - p) / (1 - p)

Selling Yes at bid ``p`` is equivalent to buying No at cost ``1 - p`` with win
probability ``1 - q``, giving ``f* = (p - q) / p``.

We then multiply by the configured Kelly fraction (e.g. 0.25) *and* the model's
calibration factor (<=1), convert the bankroll-at-risk into an integer contract
count, and hard-cap by per-market and per-match limits.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..models.schemas import EdgeSignal, OrderAction
from ..util import clamp


def kelly_fraction_for_trade(
    model_prob_yes: float, exec_price: float, action: OrderAction
) -> tuple[float, float, float]:
    """Return (full_kelly_fraction, cost_per_contract, win_prob) for the trade.

    ``cost_per_contract`` is the capital at risk per contract (``p`` for a buy,
    ``1 - p`` for a sell). The Kelly fraction is clamped to [0, 1].
    """
    p = clamp(exec_price, 1e-4, 1 - 1e-4)
    if action is OrderAction.BUY:
        kelly = (model_prob_yes - p) / (1 - p)
        cost_per_contract = p
        win_prob = model_prob_yes
    else:  # SELL yes == buy No at cost (1 - p)
        kelly = (p - model_prob_yes) / p
        cost_per_contract = 1 - p
        win_prob = 1 - model_prob_yes
    return clamp(kelly, 0.0, 1.0), cost_per_contract, win_prob


@dataclass
class SizingDecision:
    action: OrderAction | None
    contracts: int
    limit_price_cents: int
    cost_per_contract: float
    exposure_dollars: float
    full_kelly: float
    sized_fraction: float
    reason: str

    @property
    def is_trade(self) -> bool:
        return self.action is not None and self.contracts > 0


class PositionSizer:
    def __init__(
        self,
        *,
        kelly_fraction: float = 0.25,
        max_position_per_market: int = 100,
        max_exposure_per_match: float = 200.0,
        min_order_contracts: int = 1,
    ) -> None:
        self.kelly_fraction = kelly_fraction
        self.max_position_per_market = max_position_per_market
        self.max_exposure_per_match = max_exposure_per_match
        self.min_order_contracts = min_order_contracts

    @classmethod
    def from_config(cls, cfg) -> "PositionSizer":
        r = cfg.risk
        return cls(
            kelly_fraction=r.kelly_fraction,
            max_position_per_market=r.max_position_per_market,
            max_exposure_per_match=r.max_exposure_per_match,
            min_order_contracts=r.min_order_contracts,
        )

    def size(
        self,
        edge: EdgeSignal,
        bankroll: float,
        *,
        calibration_factor: float = 1.0,
        existing_contracts: int = 0,
        match_exposure: float = 0.0,
    ) -> SizingDecision:
        if edge.action is None or not edge.actionable:
            return SizingDecision(None, 0, 0, 0.0, 0.0, 0.0, 0.0, "not actionable")

        exec_price = (
            (edge.market_yes_ask or 0) / 100.0
            if edge.action is OrderAction.BUY
            else (edge.market_yes_bid or 0) / 100.0
        )
        full_kelly, cost_per_contract, _win = kelly_fraction_for_trade(
            edge.model_prob, exec_price, edge.action
        )
        sized_fraction = full_kelly * self.kelly_fraction * clamp(calibration_factor, 0.0, 1.0)
        dollars_at_risk = max(0.0, sized_fraction * bankroll)

        if cost_per_contract <= 0:
            return SizingDecision(None, 0, 0, 0.0, 0.0, full_kelly, 0.0, "degenerate price")

        contracts = int(dollars_at_risk // cost_per_contract)

        # Hard caps (belt-and-suspenders; RiskManager re-checks authoritatively).
        room_market = self.max_position_per_market - abs(existing_contracts)
        contracts = min(contracts, max(0, room_market))
        room_exposure = self.max_exposure_per_match - match_exposure
        if cost_per_contract > 0:
            contracts = min(contracts, int(max(0.0, room_exposure) // cost_per_contract))

        if contracts < self.min_order_contracts:
            return SizingDecision(
                None, 0, int(round(exec_price * 100)), cost_per_contract, 0.0,
                full_kelly, sized_fraction,
                f"sized {contracts} < min {self.min_order_contracts}",
            )

        return SizingDecision(
            action=edge.action,
            contracts=contracts,
            limit_price_cents=int(round(exec_price * 100)),
            cost_per_contract=cost_per_contract,
            exposure_dollars=contracts * cost_per_contract,
            full_kelly=full_kelly,
            sized_fraction=sized_fraction,
            reason=f"kelly {full_kelly:.3f} x frac {self.kelly_fraction} x calib -> {contracts} contracts",
        )
