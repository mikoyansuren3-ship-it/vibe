"""Per-tick processing: one MatchSnapshot (+ its market snapshots) through the
full pipeline. Shared by the live orchestrator and the backtest harness so they
are guaranteed identical.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from . import trading
from ..eventbus import Event, EventType
from ..features.engineer import match_features
from ..logging_setup import get_logger
from ..market.implied import implied_from_markets
from ..models.schemas import MarketSnapshot, MatchSnapshot, Outcome, Probabilities
from ..util import utcnow

if TYPE_CHECKING:
    from .builders import Runtime

log = get_logger("engine.tick")


def realized_outcome(match: MatchSnapshot) -> Outcome:
    d = match.score_diff
    return Outcome.HOME if d > 0 else Outcome.DRAW if d == 0 else Outcome.AWAY


@dataclass
class MatchState:
    match_id: str
    prev: MatchSnapshot | None = None
    first_prob: Probabilities | None = None
    last_trade_minute: dict[str, int] = field(default_factory=dict)
    settled: bool = False
    n_orders: int = 0
    n_fills: int = 0


class TickProcessor:
    def __init__(
        self,
        rt: "Runtime",
        *,
        trade: bool = True,
        persist: bool = True,
        decision_mode: str = "autonomous",
    ) -> None:
        self.rt = rt
        self.trade = trade
        self.persist = persist  # backtests set False to skip per-row DB writes
        # autonomous = auto-execute; advisory = queue proposals for human approval
        self.decision_mode = decision_mode
        self._last_eq = 0.0  # monotonic time of last equity-curve sample

    async def process(
        self, match: MatchSnapshot, market_snaps: list[MarketSnapshot], mstate: MatchState
    ) -> dict[str, object]:
        rt = self.rt
        cfg = rt.cfg

        # 1) Model probabilities
        probs = rt.model.predict(match)
        if self.persist:
            rt.db.add_probabilities(probs)
        rt.bus.publish(Event(EventType.PROBABILITIES, probs.model_dump(mode="json"), match.match_id))
        if mstate.first_prob is None and match.period.is_live:
            mstate.first_prob = probs

        feats = match_features(match)
        mids = {s.market_ticker: s.yes_mid_prob for s in market_snaps if s.yes_mid_prob is not None}

        # 2) Market-implied + edges
        view = implied_from_markets(market_snaps, method=cfg.edge.devig_method) if market_snaps else None
        edges = rt.detector.evaluate(probs, view) if view else []
        for edge in edges:
            if self.persist:
                rt.db.add_edge(edge)
                rt.audit.signal(edge)
            rt.bus.publish(Event(EventType.EDGE, edge.model_dump(mode="json"), match.match_id))

        # 3) Alerts (goal / red card / divergence)
        self._alerts(match, mstate, edges)

        # 4) Trading (autonomous) or proposals (advisory)
        rt.last_market_snaps.update({s.market_ticker: s for s in market_snaps})
        if self.trade and not match.period.is_finished and rt.risk.trading_allowed:
            for edge in edges:
                if edge.actionable:
                    await self._handle_edge(match, edge, market_snaps, mstate)
        trading.expire_proposals(rt)

        # 5) Mark-to-market across the WHOLE open book (all matches) + daily-loss guardrail
        rt.last_mids.update(mids)
        rt.risk.update_unrealized(rt.portfolio.unrealized_pnl(rt.last_mids))
        self._sample_equity(rt)

        # 6) Settlement
        if match.period.is_finished and not mstate.settled:
            self._settle(match, mstate)

        # 7) Publish runtime state for the dashboard
        self._update_state(match, probs, view, edges, mids)

        mstate.prev = match
        return {
            "minute": match.minute,
            "edges": len(edges),
            "actionable": sum(1 for e in edges if e.actionable),
            "model": (probs.p_home, probs.p_draw, probs.p_away),
            "features": feats,
        }

    # ------------------------------------------------------------------ #
    async def _handle_edge(self, match, edge, market_snaps, mstate) -> None:
        """Size + risk-check an actionable edge, then either propose it (advisory)
        or execute it (autonomous)."""
        rt = self.rt
        cfg = rt.cfg
        ticker = edge.market_ticker
        last = mstate.last_trade_minute.get(ticker)
        if last is not None and (match.minute - last) < cfg.execution.min_retrade_minutes:
            return

        decision = rt.sizer.size(
            edge,
            rt.portfolio.bankroll(),
            calibration_factor=rt.calibration.calibration_factor(),
            existing_contracts=rt.risk.positions.get(ticker, 0),
            match_exposure=rt.risk.match_exposure.get(match.match_id, 0.0),
        )
        if not decision.is_trade:
            return

        rd = rt.risk.pre_trade_check(
            match_id=match.match_id,
            market_ticker=ticker,
            action=decision.action,
            contracts=decision.contracts,
            cost_per_contract=decision.cost_per_contract,
            price=decision.limit_price_cents / 100.0,
        )
        if not rd.approved:
            rt.audit.guardrail(rd.reason, match_id=match.match_id, market_ticker=ticker)
            return

        # Advisory: queue a proposal for the human to approve in the dashboard.
        if self.decision_mode == "advisory":
            trading.make_or_update_proposal(
                rt, match, edge, decision, rd,
                calibration=rt.calibration.calibration_factor(), persist=self.persist,
            )
            return

        # Autonomous: execute immediately.
        snap = next((s for s in market_snaps if s.market_ticker == ticker), None)
        coid = f"{match.match_id}:{ticker}:{match.minute}:{decision.action.value}"[:60]
        _result, n_fills = await trading.place_and_book(
            rt,
            coid=coid,
            match_id=match.match_id,
            market_ticker=ticker,
            outcome=edge.outcome,
            action=decision.action,
            contracts=rd.contracts,
            limit_price_cents=decision.limit_price_cents,
            cost_per_contract=decision.cost_per_contract,
            snap=snap,
            persist=self.persist,
        )
        mstate.n_orders += 1
        if n_fills:
            mstate.n_fills += n_fills
            mstate.last_trade_minute[ticker] = match.minute

    def _sample_equity(self, rt) -> None:
        """Append a point to the equity ring buffer (live only, throttled to ~5s)."""
        if not self.persist:  # backtests don't need the live curve
            return
        import time

        now = time.monotonic()
        if now - self._last_eq < 5.0:
            return
        self._last_eq = now
        rt.equity_curve.append(
            {
                "ts": utcnow().isoformat(),
                "equity": round(rt.portfolio.equity(rt.last_mids), 2),
                "realized": round(rt.portfolio.realized_pnl, 2),
                "unrealized": round(rt.portfolio.unrealized_pnl(rt.last_mids), 2),
            }
        )

    def _alerts(self, match, mstate, edges) -> None:
        rt = self.rt
        prev = mstate.prev
        if prev is not None:
            if (match.home_score, match.away_score) != (prev.home_score, prev.away_score):
                self._alert("goal", f"GOAL {match.home_team} {match.home_score}-{match.away_score} {match.away_team}", match)
            new_reds = (match.home.red_cards + match.away.red_cards) - (
                prev.home.red_cards + prev.away.red_cards
            )
            if new_reds > 0:
                self._alert("red_card", f"RED CARD in {match.home_team} v {match.away_team} (min {match.minute})", match)
        if edges:
            worst = max(edges, key=lambda e: abs(e.raw_edge))
            if abs(worst.raw_edge) >= rt.cfg.alerts.divergence_threshold:
                self._alert(
                    "divergence",
                    f"{worst.outcome.value} model {worst.model_prob:.2f} vs market {worst.market_prob:.2f} (edge {worst.raw_edge:+.2f})",
                    match,
                )

    def _alert(self, kind: str, message: str, match) -> None:
        rt = self.rt
        rt.audit.alert(kind, message, match_id=match.match_id)
        rt.bus.publish(Event(EventType.ALERT, {"kind": kind, "message": message}, match.match_id))
        rt.state.add_decision({"kind": f"alert:{kind}", "message": message, "match_id": match.match_id})

    def _settle(self, match, mstate) -> None:
        rt = self.rt
        outcome = realized_outcome(match)
        result = f"{match.home_team} {match.home_score}-{match.away_score} {match.away_team}"
        # Settle each open market for this match, recording a bet-history entry per market.
        delta = 0.0
        for ticker, pos in list(rt.portfolio.positions.items()):
            if pos.match_id != match.match_id:
                continue
            back = pos.yes_contracts >= pos.no_contracts
            contracts = pos.yes_contracts if back else pos.no_contracts
            label = (
                match.home_team
                if pos.outcome is Outcome.HOME
                else match.away_team
                if pos.outcome is Outcome.AWAY
                else "Draw"
            )
            pnl = rt.portfolio.settle_market(ticker, yes_won=(pos.outcome is outcome))
            delta += pnl
            rt.bet_history.append(
                {
                    "ts": utcnow().isoformat(),
                    "match": f"{match.home_team} v {match.away_team}",
                    "label": label,
                    "side": "back" if back else "fade",
                    "contracts": contracts,
                    "result": f"{result} -> {outcome.value}",
                    "pnl": round(pnl, 2),
                    "won": pnl > 0,
                }
            )
        rt.bet_history[:] = rt.bet_history[-200:]
        rt.risk.record_realized_pnl(delta, match_id=match.match_id)
        if mstate.first_prob is not None:
            rt.calibration.add(mstate.first_prob, outcome)
        mstate.settled = True
        rt.audit.log(
            "settlement",
            f"{result} -> {outcome.value}, realized {delta:+.2f}",
            match_id=match.match_id,
            realized=delta,
        )
        rt.bus.publish(Event(EventType.PNL, {"realized_delta": delta, "outcome": outcome.value}, match.match_id))

    def _update_state(self, match, probs, view, edges, mids) -> None:
        rt = self.rt
        market_probs = None
        if view is not None:
            mp = view.probabilities()
            market_probs = {"home": mp.p_home, "draw": mp.p_draw, "away": mp.p_away}
        rt.state.update_match(
            match.match_id,
            {
                "match_id": match.match_id,
                "home_team": match.home_team,
                "away_team": match.away_team,
                "minute": match.minute,
                "period": match.period.value,
                "score": f"{match.home_score}-{match.away_score}",
                "xg": [round(match.home.xg, 2), round(match.away.xg, 2)],
                "red_cards": [match.home.red_cards, match.away.red_cards],
                "model": {"home": probs.p_home, "draw": probs.p_draw, "away": probs.p_away},
                "market": market_probs,
                "edges": [
                    {
                        "outcome": e.outcome.value,
                        "raw_edge": round(e.raw_edge, 3),
                        "net_edge": round(e.net_edge, 3),
                        "actionable": e.actionable,
                    }
                    for e in edges
                ],
            },
        )
        rt.state.risk = rt.risk.snapshot()
        rt.state.portfolio = rt.portfolio.snapshot(rt.last_mids)
