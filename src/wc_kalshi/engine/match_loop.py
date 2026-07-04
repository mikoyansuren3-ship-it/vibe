"""Per-tick processing: one MatchSnapshot (+ its market snapshots) through the
full pipeline. Shared by the live orchestrator and the backtest harness so they
are guaranteed identical.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from . import trading
from ..eventbus import Event, EventType
from ..logging_setup import get_logger
from ..market.implied import implied_from_markets
from ..modeling.xg_proxy import observed_xg
from ..models.schemas import MarketSnapshot, MatchSnapshot, OrderAction, Outcome, Probabilities
from ..util import utcnow

if TYPE_CHECKING:
    from .builders import Runtime

log = get_logger("engine.tick")

# Match-minute checkpoints at which we snapshot the model's in-play prediction for
# calibration. Scoring calibration ONLY on the minute-1 prediction (the prior) tells
# you nothing about the in-play predictions you actually trade on, so we pool a
# prediction from across the match timeline plus every prediction we trade on.
CALIBRATION_CHECKPOINTS: tuple[int, ...] = (10, 25, 40, 55, 70, 85)


def realized_outcome(match: MatchSnapshot) -> Outcome:
    d = match.score_diff
    return Outcome.HOME if d > 0 else Outcome.DRAW if d == 0 else Outcome.AWAY


def match_context_view(match: MatchSnapshot) -> dict | None:
    """Compact pre-match context (formations/injuries/XI) for the UI, or None."""
    c = match.context
    if c is None or not (c.home_formation or c.away_formation or c.home_injuries or c.away_injuries):
        return None
    return {
        "home_formation": c.home_formation,
        "away_formation": c.away_formation,
        "home_injuries": c.home_injuries,
        "away_injuries": c.away_injuries,
        "home_xi": c.home_xi,
        "away_xi": c.away_xi,
    }


@dataclass
class MatchState:
    match_id: str
    prev: MatchSnapshot | None = None
    first_prob: Probabilities | None = None
    # Predictions pooled for calibration: one per checkpoint minute crossed, plus
    # every prediction we actually traded on. Scored against the realized outcome
    # when the match settles.
    pred_samples: list[Probabilities] = field(default_factory=list)
    checkpoints_seen: set[int] = field(default_factory=set)
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
        defer_book_epilogue: bool = False,
    ) -> None:
        self.rt = rt
        self.trade = trade
        self.persist = persist  # backtests set False to skip per-row DB writes
        # autonomous = auto-execute; advisory = queue proposals for human approval
        self.decision_mode = decision_mode
        # When True (the live orchestrator), the whole-book mark-to-market + risk/portfolio
        # snapshots are NOT done per match inside process(); the caller runs book_epilogue()
        # ONCE per poll instead (O(positions) per poll, not O(matches × positions) per tick).
        # Backtests leave this False and keep the inline per-match behaviour unchanged.
        self._defer_epilogue = defer_book_epilogue
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
        self._sample_calibration(match, probs, mstate)

        # Strict two-sided book mids only: these become rt.last_mids — the marks behind
        # unrealized P&L, the daily-loss halt, and position stops. A one-sided book
        # keeps its previous mark rather than adopting a stale last-trade print.
        mids = {
            s.market_ticker: s.yes_book_mid_prob
            for s in market_snaps
            if s.yes_book_mid_prob is not None
        }

        # 2) Market-implied + edges
        view = implied_from_markets(market_snaps, method=cfg.edge.devig_method) if market_snaps else None
        edges = rt.detector.evaluate(probs, view) if view else []
        if self.persist and edges:
            rt.db.add_edges(edges)  # one txn for the tick's 1X2 edges
            rt.audit.signals(edges)  # one file append + one decisions txn
        for edge in edges:
            rt.bus.publish(Event(EventType.EDGE, edge.model_dump(mode="json"), match.match_id))

        # 3) Alerts (goal / red card / divergence)
        self._alerts(match, mstate, edges)

        # 4) Trading (autonomous) or proposals (advisory)
        rt.last_market_snaps.update({s.market_ticker: s for s in market_snaps})
        if self.trade and not match.period.is_finished and rt.risk.trading_allowed:
            actionable = [e for e in edges if e.actionable]
            # Joint 1X2: the three legs are mutually exclusive outcomes of ONE event,
            # so independent per-leg Kelly over-concentrates match risk. Act on the
            # single strongest leg per tick (the per-match dollar cap is the backstop).
            if actionable and getattr(cfg.execution, "one_trade_per_match_tick", True):
                actionable = [max(actionable, key=lambda e: abs(e.net_edge))]
            for edge in actionable:
                await self._handle_edge(match, edge, market_snaps, mstate, probs)
        trading.expire_proposals(rt)

        # 5) Mark-to-market across the WHOLE open book (all matches) + daily-loss guardrail.
        # ``mids`` are the REAL book mids from the market snapshots, so the unrealized
        # P&L the daily-loss halt keys off is a true mark, not the engine's own price.
        rt.last_mids.update(mids)  # per-match: this fixture contributes its fresh marks
        # The whole-book unrealized mark + daily-loss check is O(open positions) and identical
        # for every match in a poll, so in live mode the orchestrator runs it ONCE per poll via
        # book_epilogue(); only backtests (sequential) do it inline here. Position stops below
        # read last_mids directly and stay per-match either way.
        if not self._defer_epilogue:
            rt.risk.update_unrealized(rt.portfolio.unrealized_pnl(rt.last_mids))
        # Position-level stops: flatten a market whose mark-to-market loss has run past
        # the configured fraction of cost, before it can decay further to settlement.
        if self.trade and match.period.is_live:
            await self._check_position_stops(match, market_snaps)
        if not self._defer_epilogue:
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
        }

    def book_epilogue(self) -> None:
        """Whole-book mark-to-market + daily-loss check + dashboard risk/portfolio snapshots
        + equity sample. In live mode the orchestrator calls this ONCE per poll (after the
        match gather), replacing the identical per-match work that ran inside process() —
        these are all whole-book quantities, so doing them per match was O(matches ×
        positions) of redundant recompute each tick.

        The daily-loss halt this feeds therefore updates once per poll rather than mid-poll;
        the per-trade / per-match dollar caps (checked under the trade lock) remain the
        real-time guardrails. Only used when ``defer_book_epilogue`` was set; backtests keep
        doing this inline per process() call, so their halt/equity behaviour is unchanged."""
        rt = self.rt
        rt.risk.update_unrealized(rt.portfolio.unrealized_pnl(rt.last_mids))
        rt.state.risk = rt.risk.snapshot()
        rt.state.portfolio = rt.portfolio.snapshot(rt.last_mids)
        self._sample_equity(rt)

    def _sample_calibration(self, match, probs, mstate) -> None:
        """Pool a prediction at each checkpoint minute crossed (in-play, not just
        the prior) so Brier/ECE describe predictions across the match timeline."""
        if not match.period.is_live:
            return
        for cp in CALIBRATION_CHECKPOINTS:
            if cp not in mstate.checkpoints_seen and match.minute >= cp:
                mstate.checkpoints_seen.add(cp)
                mstate.pred_samples.append(probs)

    # ------------------------------------------------------------------ #
    async def _handle_edge(self, match, edge, market_snaps, mstate, probs=None) -> None:
        """Size + risk-check an actionable edge, then either propose it (advisory)
        or execute it (autonomous)."""
        rt = self.rt
        cfg = rt.cfg
        ticker = edge.market_ticker
        last = mstate.last_trade_minute.get(ticker)
        if last is not None and (match.minute - last) < cfg.execution.min_retrade_minutes:
            return

        # Adverse-selection guard: if the price we'd transact at has moved against us
        # versus the signal price beyond tolerance, skip — we only get filled when wrong.
        if self._adversely_selected(edge, market_snaps):
            rt.audit.guardrail("adverse selection: price moved against signal", match_id=match.match_id, market_ticker=ticker)
            return

        # Late-game exposure taper: variance collapses near settlement, so shrink size
        # as the match winds down (a 3c edge at minute 89 is not a 3c edge at minute 20).
        taper = self._late_game_taper(match)
        decision = rt.sizer.size(
            edge,
            rt.portfolio.bankroll(),
            calibration_factor=rt.calibration.calibration_factor() * taper,
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
            minute=match.minute,
            risk_check=True,  # re-checked under the lock; the rd above is advisory
        )
        mstate.n_orders += 1
        if n_fills:
            mstate.n_fills += n_fills
            mstate.last_trade_minute[ticker] = match.minute
            # Score calibration on exactly the prediction we traded on.
            if probs is not None:
                mstate.pred_samples.append(probs)

    def _adversely_selected(self, edge, market_snaps) -> bool:
        """True if the executable price moved against us beyond tolerance since the
        signal (we only get filled when we're wrong). Compares the edge's signal price to
        the latest book on the trade side.

        Fails CLOSED when the current price can't be verified (no quote for the market this
        tick, or our side of the book vanished): firing blind there IS the adverse case.
        In a backtest the signal price IS this tick's quote, so those branches never
        trigger — this only tightens live behaviour. A genuinely absent signal price
        (e.g. synthetic feeds) leaves the guard inapplicable (allow)."""
        tol = getattr(self.rt.cfg.execution, "max_adverse_cents", 0)
        if not tol or tol <= 0 or edge.action is None:
            return False
        snap = next((s for s in market_snaps if s.market_ticker == edge.market_ticker), None)
        if snap is None:
            return True  # no current quote — can't confirm the price held; don't fire blind
        if edge.action is OrderAction.BUY:
            signal, now = edge.market_yes_ask, snap.yes_ask
        else:
            signal, now = edge.market_yes_bid, snap.yes_bid
        if signal is None:
            return False  # no signal price to compare against — guard inapplicable
        if now is None:
            return True  # our side of the book vanished — can't verify; fail closed
        return now > signal + tol if edge.action is OrderAction.BUY else now < signal - tol

    def _late_game_taper(self, match) -> float:
        """Shrink size toward a floor as the match nears full time."""
        window = getattr(self.rt.cfg.execution, "late_taper_minutes", 0)
        if not window or window <= 0:
            return 1.0
        floor = getattr(self.rt.cfg.execution, "late_taper_floor", 0.3)
        rem = match.minutes_remaining
        if rem >= window:
            return 1.0
        return float(max(floor, min(1.0, rem / window)))

    async def _check_position_stops(self, match, market_snaps) -> None:
        """Flatten any market for this match whose unrealized loss exceeds the stop.

        Loss is measured on the real book mid versus cost paid. Mutually-exclusive 1X2
        legs are stopped independently; netting then frees the capital.
        """
        rt = self.rt
        thr = rt.cfg.risk.position_stop_loss
        if not thr or thr <= 0:
            return
        for ticker, pos in list(rt.portfolio.positions.items()):
            if pos.match_id != match.match_id:
                continue
            net = pos.net_yes
            if net == 0 or pos.cost_paid <= 0:
                continue
            mid = rt.last_mids.get(ticker)
            if mid is None:
                continue
            unrealized = pos.value_at(mid) - pos.cost_paid
            if unrealized >= -thr * pos.cost_paid:
                continue
            action = OrderAction.SELL if net > 0 else OrderAction.BUY
            snap = next((s for s in market_snaps if s.market_ticker == ticker), None)
            price = trading._closing_price_cents(snap, action)
            coid = f"stop:{ticker}:{match.minute}"[:60]
            _result, n_fills = await trading.place_and_book(
                rt,
                coid=coid,
                match_id=match.match_id,
                market_ticker=ticker,
                outcome=pos.outcome,
                action=action,
                contracts=abs(net),
                limit_price_cents=price,
                cost_per_contract=(price if action is OrderAction.BUY else 100 - price) / 100.0,
                snap=snap,
                persist=self.persist,
                minute=match.minute,
            )
            if n_fills:
                rt.audit.guardrail(
                    f"position stop: {ticker} unrealized {unrealized:.2f} "
                    f"(< -{thr:.0%} of {pos.cost_paid:.2f})",
                    match_id=match.match_id,
                    market_ticker=ticker,
                )

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
        # Score every pooled in-play/traded prediction against the realized outcome
        # (fall back to the first live prediction if no checkpoint was ever crossed).
        samples = mstate.pred_samples or ([mstate.first_prob] if mstate.first_prob else [])
        for pred in samples:
            rt.calibration.add(pred, outcome)
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
                "xg": [round(observed_xg(match.home) or 0.0, 2),
                       round(observed_xg(match.away) or 0.0, 2)],
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
                "context": match_context_view(match),
            },
        )
        # Whole-book snapshots are per-POLL in live mode (book_epilogue); the per-match
        # update_match above always runs so each fixture's own card stays fresh.
        if not self._defer_epilogue:
            rt.state.risk = rt.risk.snapshot()
            rt.state.portfolio = rt.portfolio.snapshot(rt.last_mids)
