"""Portfolio accounting: cash, positions, mark-to-market, and settlement.

Model: every trade is a *purchase* of a contract, so cash only ever decreases on
entry and increases at settlement when the winning side pays $1.
  * BUY Yes  @ p   -> buy a Yes contract for p           (wins if outcome happens)
  * SELL Yes @ p   -> buy a No  contract for (1 - p)     (wins if it does NOT)

This keeps cash arithmetic exact and never frees capital that wasn't locked. A
known simplification: opposing trades in one market accumulate offsetting Yes/No
lots rather than netting to cash; the exposure guardrails bound how much that can
tie up. P&L (realized at settlement, unrealized marked to the Yes mid) is exact.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..models.schemas import OrderAction, Outcome


@dataclass
class Position:
    market_ticker: str
    match_id: str
    outcome: Outcome
    yes_contracts: int = 0
    no_contracts: int = 0
    cost_paid: float = 0.0  # total cash paid (incl. fees) for this market
    # Cost split by side (incl. allocated fees) so offsetting lots can be netted.
    yes_cost: float = 0.0
    no_cost: float = 0.0

    @property
    def net_yes(self) -> int:
        return self.yes_contracts - self.no_contracts

    def value_at(self, yes_mid_prob: float) -> float:
        return self.yes_contracts * yes_mid_prob + self.no_contracts * (1.0 - yes_mid_prob)


@dataclass
class Portfolio:
    starting_bankroll: float = 1000.0
    cash: float = field(default=None)  # type: ignore[assignment]
    positions: dict[str, Position] = field(default_factory=dict)
    realized_pnl: float = 0.0
    fees_paid: float = 0.0
    # Net offsetting Yes/No lots in the same market into guaranteed $1 pairs, realizing
    # them immediately to free tied-up capital (a Yes+No pair always pays exactly $1).
    net_offsetting: bool = True

    def __post_init__(self) -> None:
        if self.cash is None:
            self.cash = self.starting_bankroll

    def _position(self, market_ticker: str, match_id: str, outcome: Outcome) -> Position:
        if market_ticker not in self.positions:
            self.positions[market_ticker] = Position(market_ticker, match_id, outcome)
        return self.positions[market_ticker]

    def apply_fill(
        self,
        *,
        match_id: str,
        market_ticker: str,
        outcome: Outcome,
        action: OrderAction,
        contracts: int,
        price_cents: int,
        fee: float,
    ) -> None:
        """``price_cents`` is always the YES-side price; a SELL buys No at 100-price."""
        pos = self._position(market_ticker, match_id, outcome)
        if action is OrderAction.BUY:
            cost = contracts * price_cents / 100.0
            pos.yes_contracts += contracts
            pos.yes_cost += cost + fee
        else:
            cost = contracts * (100 - price_cents) / 100.0
            pos.no_contracts += contracts
            pos.no_cost += cost + fee
        self.cash -= cost + fee
        pos.cost_paid += cost + fee
        self.fees_paid += fee
        if self.net_offsetting:
            self._net_offsetting(pos)

    def _net_offsetting(self, pos: Position) -> None:
        """Collapse matched Yes/No lots into realized cash now.

        A matched Yes+No pair pays exactly $1 at settlement regardless of result, so
        holding both ties up capital for no reason. We realize ``matched * $1`` against
        the average cost of those lots immediately and remove them, freeing exposure.
        """
        matched = min(pos.yes_contracts, pos.no_contracts)
        if matched <= 0:
            return
        avg_yes = pos.yes_cost / pos.yes_contracts
        avg_no = pos.no_cost / pos.no_contracts
        freed_cost = matched * (avg_yes + avg_no)
        self.cash += matched * 1.0
        self.realized_pnl += matched * 1.0 - freed_cost
        pos.yes_contracts -= matched
        pos.no_contracts -= matched
        pos.yes_cost -= matched * avg_yes
        pos.no_cost -= matched * avg_no
        pos.cost_paid -= freed_cost
        if pos.yes_contracts == 0 and pos.no_contracts == 0:
            self.positions.pop(pos.market_ticker, None)

    # -- valuation ------------------------------------------------------- #
    def bankroll(self) -> float:
        """Conservative bankroll for Kelly sizing: starting + realized only."""
        return max(0.0, self.starting_bankroll + self.realized_pnl)

    def holdings_value(self, yes_mids: dict[str, float]) -> float:
        total = 0.0
        for ticker, pos in self.positions.items():
            mid = yes_mids.get(ticker)
            if mid is not None:
                total += pos.value_at(mid)
        return total

    def equity(self, yes_mids: dict[str, float]) -> float:
        return self.cash + self.holdings_value(yes_mids)

    def unrealized_pnl(self, yes_mids: dict[str, float]) -> float:
        return self.equity(yes_mids) - self.starting_bankroll - self.realized_pnl

    # -- settlement ------------------------------------------------------ #
    def settle_market(self, market_ticker: str, yes_won: bool) -> float:
        pos = self.positions.get(market_ticker)
        if pos is None:
            return 0.0
        payout = pos.yes_contracts if yes_won else pos.no_contracts
        self.cash += payout
        pnl = payout - pos.cost_paid
        self.realized_pnl += pnl
        del self.positions[market_ticker]
        return pnl

    def settle_match(self, match_id: str, realized_outcome: Outcome) -> float:
        """Settle every market for a finished match. Returns total realized delta."""
        total = 0.0
        for ticker, pos in list(self.positions.items()):
            if pos.match_id != match_id:
                continue
            total += self.settle_market(ticker, yes_won=(pos.outcome is realized_outcome))
        return total

    def snapshot(self, yes_mids: dict[str, float] | None = None) -> dict[str, object]:
        mids = yes_mids or {}
        return {
            "cash": round(self.cash, 2),
            "realized_pnl": round(self.realized_pnl, 2),
            "unrealized_pnl": round(self.unrealized_pnl(mids), 2),
            "equity": round(self.equity(mids), 2),
            "fees_paid": round(self.fees_paid, 2),
            "bankroll": round(self.bankroll(), 2),
            "open_positions": {
                t: {"yes": p.yes_contracts, "no": p.no_contracts, "net_yes": p.net_yes}
                for t, p in self.positions.items()
                if p.yes_contracts or p.no_contracts
            },
        }
