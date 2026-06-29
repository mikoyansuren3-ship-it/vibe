"""Edge detector.

For each outcome we report the headline divergence (model vs de-vigged market mid)
*and* judge tradability on the price we would actually transact at: we BUY Yes at
the ask and SELL Yes at the bid, so the spread is paid implicitly. From that
executable edge we further subtract the Kalshi fee and an assumed slippage. A
signal is only actionable when this cost-adjusted edge clears the configured
threshold and the price sits inside the tradable band.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..fees import kalshi_fee
from ..market.implied import MarketView
from ..models.schemas import EdgeSignal, OrderAction, Outcome, Probabilities

if TYPE_CHECKING:
    from ..config import AppConfig

_OUTCOMES = (Outcome.HOME, Outcome.DRAW, Outcome.AWAY)


def market_pooled(model: Probabilities, market: MarketView, w: float) -> dict[Outcome, float]:
    """Log-opinion pool of the model with the de-vigged market: p ∝ p_model**w · p_market**(1-w).

    ``w=1`` returns the model unchanged (today's behaviour); ``w=0`` returns the market.
    Only pools when the FULL 1X2 book is de-vigged (all three outcomes present) — with a
    partial book there's no coherent market vector to shrink toward, so we keep the model.
    """
    probs = {o: model.get(o) for o in _OUTCOMES}
    if w >= 1.0 or not all(o in market.outcomes for o in _OUTCOMES):
        return probs
    pooled = {
        o: max(probs[o], 1e-9) ** w * max(market.outcomes[o].implied_prob, 1e-9) ** (1.0 - w)
        for o in _OUTCOMES
    }
    z = sum(pooled.values())
    return {o: pooled[o] / z for o in _OUTCOMES} if z > 0 else probs


class EdgeDetector:
    def __init__(
        self,
        *,
        min_edge: float = 0.04,
        min_edge_after_costs: float = 0.02,
        slippage_cents: int = 1,
        fee_coefficient: float = 0.07,
        maker_fraction: float = 0.25,
        min_price: float = 0.03,
        max_price: float = 0.97,
        market_pool_weight: float = 1.0,
    ) -> None:
        self.min_edge = min_edge
        self.min_edge_after_costs = min_edge_after_costs
        self.slippage = slippage_cents / 100.0
        self.fee_coefficient = fee_coefficient
        self.maker_fraction = maker_fraction
        self.min_price = min_price
        self.max_price = max_price
        self.market_pool_weight = market_pool_weight

    @classmethod
    def from_config(cls, cfg: "AppConfig") -> "EdgeDetector":
        return cls(
            min_edge=cfg.edge.min_edge,
            min_edge_after_costs=cfg.edge.min_edge_after_costs,
            slippage_cents=cfg.edge.slippage_cents,
            fee_coefficient=cfg.kalshi.fee_coefficient,
            maker_fraction=cfg.kalshi.maker_fee_fraction,
            min_price=cfg.risk.min_price,
            max_price=cfg.risk.max_price,
            market_pool_weight=cfg.edge.market_pool_weight,
        )

    def evaluate(self, model: Probabilities, market: MarketView) -> list[EdgeSignal]:
        # Shrink the model toward the (sharper) de-vigged market before judging edges, so
        # we only act on the residual the model adds. At weight 1.0 this is a no-op.
        eff = market_pooled(model, market, self.market_pool_weight)
        signals: list[EdgeSignal] = []
        for outcome, om in market.outcomes.items():
            signals.append(self._evaluate_one(outcome, eff.get(outcome, model.get(outcome)), model.match_id, om, market))
        return signals

    def _evaluate_one(self, outcome: Outcome, model_p: float, match_id: str, om, market) -> EdgeSignal:
        implied = om.implied_prob
        raw_edge = model_p - implied
        ask = om.yes_ask / 100.0 if om.yes_ask is not None else None
        bid = om.yes_bid / 100.0 if om.yes_bid is not None else None

        action: OrderAction | None = None
        exec_price: float | None = None
        net_edge = 0.0

        if raw_edge > 0 and ask is not None:
            # Model says Yes underpriced -> BUY Yes at the ask.
            # Exact ceil-rounded per-contract fee (Kalshi rounds the order fee UP to the
            # whole cent), not the un-rounded approximation — this is the real cost a
            # marginal contract clears, so a thin "edge" that only existed because we
            # under-counted the rounded fee is correctly rejected here.
            fee = kalshi_fee(1, ask, coefficient=self.fee_coefficient)
            net_edge = model_p - ask - fee - self.slippage
            action, exec_price = OrderAction.BUY, ask
        elif raw_edge < 0 and bid is not None:
            # Model says Yes overpriced -> SELL Yes at the bid.
            fee = kalshi_fee(1, bid, coefficient=self.fee_coefficient)
            net_edge = bid - model_p - fee - self.slippage
            action, exec_price = OrderAction.SELL, bid

        gross = abs(raw_edge)
        est_cost = max(0.0, gross - net_edge)

        actionable = bool(
            action is not None
            and exec_price is not None
            and net_edge >= self.min_edge_after_costs
            and gross >= self.min_edge
            and self.min_price <= exec_price <= self.max_price
            and om.market_ticker
        )
        reason = self._reason(action, exec_price, net_edge, gross, om)

        return EdgeSignal(
            match_id=match_id,
            outcome=outcome,
            market_ticker=om.market_ticker,
            model_prob=model_p,
            market_prob=implied,
            market_yes_ask=om.yes_ask,
            market_yes_bid=om.yes_bid,
            raw_edge=raw_edge,
            est_cost=est_cost,
            net_edge=net_edge,
            action=action if actionable else None,
            actionable=actionable,
            reason=reason,
        )

    def _reason(self, action, exec_price, net_edge, gross, om) -> str:
        if action is None:
            return "no executable quote on the favourable side"
        if gross < self.min_edge:
            return f"raw edge {gross:.3f} < min_edge {self.min_edge:.3f}"
        if net_edge < self.min_edge_after_costs:
            return f"net edge {net_edge:.3f} < threshold {self.min_edge_after_costs:.3f} after costs"
        if exec_price is not None and not (self.min_price <= exec_price <= self.max_price):
            return f"price {exec_price:.2f} outside tradable band"
        return f"actionable: {action.value} yes @ {exec_price:.2f}, net edge {net_edge:.3f}"
