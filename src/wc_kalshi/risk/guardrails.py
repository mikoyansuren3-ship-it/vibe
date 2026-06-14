"""Always-on risk guardrails + global kill switch.

Every order passes ``pre_trade_check`` before it can reach an executor. The manager
tracks per-market positions, per-match exposure, total open exposure, and the day's
realized+unrealized P&L, and will:

  * clamp an order down to fit remaining limits, or reject it;
  * HALT all new trading when the daily loss / drawdown limit is breached;
  * flatten-and-stop when the kill switch is engaged.

These checks are independent of the sizer (defence in depth): even a buggy sizer
cannot breach a hard limit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from ..models.schemas import OrderAction
from ..util import utcnow


@dataclass
class RiskDecision:
    approved: bool
    contracts: int
    reason: str
    halted: bool = False


@dataclass
class RiskLimits:
    max_position_per_market: int = 100
    max_exposure_per_match: float = 200.0
    max_total_open_exposure: float = 1000.0
    max_daily_loss: float = 250.0
    min_price: float = 0.03
    max_price: float = 0.97
    min_order_contracts: int = 1

    @classmethod
    def from_config(cls, cfg) -> "RiskLimits":
        r = cfg.risk
        return cls(
            max_position_per_market=r.max_position_per_market,
            max_exposure_per_match=r.max_exposure_per_match,
            max_total_open_exposure=r.max_total_open_exposure,
            max_daily_loss=r.max_daily_loss,
            min_price=r.min_price,
            max_price=r.max_price,
            min_order_contracts=r.min_order_contracts,
        )


@dataclass
class RiskManager:
    limits: RiskLimits = field(default_factory=RiskLimits)
    positions: dict[str, int] = field(default_factory=dict)  # ticker -> signed contracts
    match_exposure: dict[str, float] = field(default_factory=dict)  # match_id -> $ at risk
    realized_pnl_today: float = 0.0
    unrealized_pnl: float = 0.0
    _day: date = field(default_factory=lambda: utcnow().date())
    halted: bool = False
    halt_reason: str = ""
    kill_switch_engaged: bool = False
    on_halt: object = None  # optional callback(reason: str)

    # -- daily rollover -------------------------------------------------- #
    def _rollover_if_new_day(self) -> None:
        today = utcnow().date()
        if today != self._day:
            self._day = today
            self.realized_pnl_today = 0.0
            if not self.kill_switch_engaged:
                self.halted = False
                self.halt_reason = ""

    @property
    def total_open_exposure(self) -> float:
        return sum(self.match_exposure.values())

    @property
    def trading_allowed(self) -> bool:
        return not self.halted and not self.kill_switch_engaged

    # -- the gate -------------------------------------------------------- #
    def pre_trade_check(
        self,
        *,
        match_id: str,
        market_ticker: str,
        action: OrderAction,
        contracts: int,
        cost_per_contract: float,
        price: float,
    ) -> RiskDecision:
        self._rollover_if_new_day()

        if self.kill_switch_engaged:
            return RiskDecision(False, 0, "kill switch engaged", halted=True)
        if self.halted:
            return RiskDecision(False, 0, f"trading halted: {self.halt_reason}", halted=True)
        if contracts <= 0:
            return RiskDecision(False, 0, "zero contracts")
        if not (self.limits.min_price <= price <= self.limits.max_price):
            return RiskDecision(False, 0, f"price {price:.2f} outside tradable band")

        allowed = contracts

        # 1) per-market position cap: keep |net position| <= cap after the trade.
        #    BUY adds to position (room = cap - current);
        #    SELL subtracts (room = cap + current).
        current = self.positions.get(market_ticker, 0)
        cap = self.limits.max_position_per_market
        room_market = cap - current if action is OrderAction.BUY else cap + current
        allowed = max(0, min(allowed, room_market))

        # 2) per-match exposure cap
        room_match = self.limits.max_exposure_per_match - self.match_exposure.get(match_id, 0.0)
        if cost_per_contract > 0:
            allowed = min(allowed, int(max(0.0, room_match) // cost_per_contract))

        # 3) total open exposure cap
        room_total = self.limits.max_total_open_exposure - self.total_open_exposure
        if cost_per_contract > 0:
            allowed = min(allowed, int(max(0.0, room_total) // cost_per_contract))

        if allowed < self.limits.min_order_contracts:
            return RiskDecision(False, 0, "no room within limits (capped to 0)")
        reason = "approved" if allowed == contracts else f"approved (clamped {contracts}->{allowed})"
        return RiskDecision(True, allowed, reason)

    # -- bookkeeping ----------------------------------------------------- #
    def register_fill(
        self,
        *,
        match_id: str,
        market_ticker: str,
        action: OrderAction,
        contracts: int,
        cost_per_contract: float,
    ) -> None:
        delta = contracts if action is OrderAction.BUY else -contracts
        self.positions[market_ticker] = self.positions.get(market_ticker, 0) + delta
        self.match_exposure[match_id] = (
            self.match_exposure.get(match_id, 0.0) + contracts * cost_per_contract
        )

    def record_realized_pnl(self, amount: float, *, match_id: str | None = None) -> None:
        self._rollover_if_new_day()
        self.realized_pnl_today += amount
        if match_id is not None:
            self.match_exposure.pop(match_id, None)
        self._check_daily_loss()

    def update_unrealized(self, unrealized: float) -> None:
        self.unrealized_pnl = unrealized
        self._check_daily_loss()

    def _check_daily_loss(self) -> None:
        day_pnl = self.realized_pnl_today + self.unrealized_pnl
        if day_pnl <= -abs(self.limits.max_daily_loss) and not self.halted:
            self.trip_halt(
                f"daily loss limit hit: P&L {day_pnl:.2f} <= -{self.limits.max_daily_loss:.2f}"
            )

    # -- circuit breakers ------------------------------------------------ #
    def trip_halt(self, reason: str) -> None:
        self.halted = True
        self.halt_reason = reason
        if callable(self.on_halt):
            self.on_halt(reason)

    def engage_kill_switch(self, reason: str = "manual kill switch") -> None:
        self.kill_switch_engaged = True
        self.trip_halt(reason)

    def snapshot(self) -> dict[str, object]:
        return {
            "trading_allowed": self.trading_allowed,
            "halted": self.halted,
            "halt_reason": self.halt_reason,
            "kill_switch": self.kill_switch_engaged,
            "realized_pnl_today": round(self.realized_pnl_today, 2),
            "unrealized_pnl": round(self.unrealized_pnl, 2),
            "total_open_exposure": round(self.total_open_exposure, 2),
            "open_positions": {k: v for k, v in self.positions.items() if v != 0},
        }
