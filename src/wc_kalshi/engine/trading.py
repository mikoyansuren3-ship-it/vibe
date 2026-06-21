"""Shared trade execution + advisory proposals.

Both decision paths funnel through here so they behave identically:
  * autonomous -> ``place_and_book`` runs immediately when an edge is actionable.
  * advisory   -> ``make_or_update_proposal`` queues a ``TradeProposal``; the dashboard
                  later calls ``execute_proposal`` (approve) or ``reject_proposal``.

``place_and_book`` holds ``rt.trade_lock`` so the auto-loop and a dashboard approval
can never interleave a position/risk mutation.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Any

from ..eventbus import Event, EventType
from ..execution.base import OrderRequest
from ..models.schemas import (
    EdgeSignal,
    MarketSnapshot,
    MatchSnapshot,
    OrderAction,
    Outcome,
    ProposalStatus,
    TradeProposal,
)
from ..util import new_id, utcnow

if TYPE_CHECKING:
    from .builders import Runtime


def _outcome_label(match: MatchSnapshot, outcome: Outcome) -> str:
    return {
        Outcome.HOME: match.home_team,
        Outcome.DRAW: "a draw",
        Outcome.AWAY: match.away_team,
    }[outcome]


def build_thesis(
    match: MatchSnapshot, edge: EdgeSignal, contracts: int, max_loss: float, max_gain: float
) -> tuple[str, str]:
    """Human-readable rationale (incentive) + risk note for a proposal."""
    label = _outcome_label(match, edge.outcome)
    verb = "back" if edge.action is OrderAction.BUY else "fade"
    xg_h, xg_a = match.home.xg, match.away.xg
    drivers: list[str] = []
    if abs(xg_h - xg_a) >= 0.3:
        leader = match.home_team if xg_h > xg_a else match.away_team
        drivers.append(
            f"{leader} is creating the better chances (xG {xg_h:.2f}–{xg_a:.2f}) "
            f"beyond what the {match.home_score}-{match.away_score} score shows"
        )
    if match.net_red_cards != 0:
        up = match.home_team if match.net_red_cards > 0 else match.away_team
        drivers.append(f"{up} has a man advantage")
    if not drivers:
        drivers.append(f"score {match.home_score}-{match.away_score} with {match.minutes_remaining:.0f}' left")
    thesis = (
        f"{verb.capitalize()} {label}: model {edge.model_prob:.0%} vs market {edge.market_prob:.0%} "
        f"({edge.raw_edge:+.1%}). " + "; ".join(drivers).capitalize() + ". "
        f"Net edge after costs ≈ {edge.net_edge:+.1%}."
    )
    risk = (
        f"Max loss ${max_loss:.2f} if it loses; max gain ${max_gain:.2f} if it wins. "
        f"{contracts} contracts."
    )
    return thesis, risk


def make_or_update_proposal(
    rt: "Runtime",
    match: MatchSnapshot,
    edge: EdgeSignal,
    decision: Any,
    rd: Any,
    *,
    calibration: float,
    persist: bool = True,
) -> TradeProposal:
    """Create (or refresh in place) the pending proposal for this market."""
    ticker = edge.market_ticker
    contracts = rd.contracts
    cost = decision.cost_per_contract
    exposure = contracts * cost
    max_loss = exposure
    max_gain = contracts * (1.0 - cost)
    ev = contracts * edge.net_edge
    thesis, risk_note = build_thesis(match, edge, contracts, max_loss, max_gain)
    expires = utcnow() + timedelta(seconds=rt.cfg.execution.proposal_ttl_seconds)

    fields = dict(
        ts=utcnow(),
        minute=match.minute,
        score=f"{match.home_score}-{match.away_score}",
        market_ticker=ticker,
        outcome=edge.outcome,
        action=edge.action,
        model_prob=edge.model_prob,
        market_prob=edge.market_prob,
        raw_edge=edge.raw_edge,
        net_edge=edge.net_edge,
        expected_value=ev,
        max_gain=max_gain,
        max_loss=max_loss,
        contracts=contracts,
        limit_price_cents=decision.limit_price_cents,
        cost_per_contract=cost,
        exposure_dollars=exposure,
        kelly_fraction=decision.full_kelly,
        calibration_factor=calibration,
        thesis=thesis,
        risk_note=risk_note,
        expires_ts=expires,
    )

    existing = next(
        (p for p in rt.proposals.values() if p.market_ticker == ticker and p.is_pending), None
    )
    if existing is not None:
        for k, v in fields.items():
            setattr(existing, k, v)
        return existing

    proposal = TradeProposal(
        id=new_id("prop-"),
        match_id=match.match_id,
        home_team=match.home_team,
        away_team=match.away_team,
        **fields,
    )
    rt.proposals[proposal.id] = proposal
    msg = f"PROPOSED {edge.action.value} {contracts} {ticker} @ {decision.limit_price_cents}c (edge {edge.net_edge:+.3f})"
    rt.state.add_decision({"kind": "proposal", "match_id": match.match_id, "message": msg})
    rt.bus.publish(Event(EventType.ALERT, {"kind": "proposal", "message": msg}, match.match_id))
    if persist:
        rt.audit.log(
            "proposal", thesis, match_id=match.match_id, proposal_id=proposal.id,
            ticker=ticker, contracts=contracts, net_edge=edge.net_edge,
        )
    return proposal


async def place_and_book(
    rt: "Runtime",
    *,
    coid: str,
    match_id: str,
    market_ticker: str,
    outcome: Outcome,
    action: OrderAction,
    contracts: int,
    limit_price_cents: int,
    cost_per_contract: float,
    snap: MarketSnapshot | None,
    persist: bool = True,
    minute: int | None = None,
) -> tuple[Any, int]:
    """Place an order and book any fills into portfolio + risk. Returns (result, n_fills).

    ``snap`` is the latest MARKET snapshot (used to model the fill); ``minute`` is the
    match minute, recorded for CLV/diagnostics.
    """
    order = OrderRequest(
        match_id=match_id,
        market_ticker=market_ticker,
        outcome=outcome,
        action=action,
        contracts=contracts,
        limit_price_cents=limit_price_cents,
        cost_per_contract=cost_per_contract,
        time_in_force=rt.cfg.execution.order_time_in_force,
        client_order_id=coid,
    )
    n_fills = 0
    async with rt.trade_lock:
        result = await rt.executor.place(order, snap)
        if persist:
            _persist_order(rt, order, result)
            rt.audit.order(order, result)
        rt.bus.publish(Event(EventType.ORDER, {"coid": coid, "status": result.status.value}, match_id))
        if result.is_filled:
            for fill in result.fills:
                rt.portfolio.apply_fill(
                    match_id=match_id,
                    market_ticker=market_ticker,
                    outcome=outcome,
                    action=fill.action,
                    contracts=fill.contracts,
                    price_cents=fill.price_cents,
                    fee=fill.fee,
                )
                rt.risk.register_fill(
                    match_id=match_id,
                    market_ticker=market_ticker,
                    action=fill.action,
                    contracts=fill.contracts,
                    cost_per_contract=cost_per_contract,
                )
                # Record the fill for closing-line-value (CLV) analysis.
                rt.fills_log.append(
                    {
                        "match_id": match_id,
                        "market_ticker": market_ticker,
                        "outcome": outcome.value,
                        "action": fill.action.value,
                        "contracts": fill.contracts,
                        "entry_price_cents": fill.price_cents,
                        "minute": minute,
                    }
                )
                if persist:
                    _persist_fill(rt, fill)
                n_fills += 1
            fmsg = f"{action.value} {result.filled_contracts} {market_ticker} @ {limit_price_cents}c"
            rt.state.add_decision({"kind": "fill", "match_id": match_id, "message": fmsg})
            rt.bus.publish(Event(EventType.ALERT, {"kind": "fill", "message": fmsg}, match_id))
        # Track an unfilled/partially-filled order as resting (live order lifecycle).
        if result.exchange_order_id and result.status.value in {"accepted", "partial"}:
            rt.resting_orders[result.exchange_order_id] = {
                "coid": coid,
                "match_id": match_id,
                "market_ticker": market_ticker,
                "placed_ts": utcnow(),
            }
    return result, n_fills


async def execute_proposal(
    rt: "Runtime", proposal_id: str, *, contracts: int | None = None, persist: bool = True
) -> tuple[bool, str]:
    """Approve + execute a pending proposal (re-checking risk at execution time).

    ``contracts`` optionally overrides the proposed size (size up/down from the UI);
    the risk manager still clamps it to the configured limits.
    """
    p: TradeProposal | None = rt.proposals.get(proposal_id)
    if p is None:
        return False, "unknown proposal"
    if not p.is_pending:
        return False, f"proposal already {p.status.value}"
    if not rt.risk.trading_allowed:
        p.status = ProposalStatus.REJECTED
        p.risk_note = "trading halted / kill switch engaged"
        return False, "trading not allowed"

    requested = p.contracts if contracts is None else max(1, int(contracts))
    rd = rt.risk.pre_trade_check(
        match_id=p.match_id,
        market_ticker=p.market_ticker,
        action=p.action,
        contracts=requested,
        cost_per_contract=p.cost_per_contract,
        price=p.limit_price_cents / 100.0,
    )
    if not rd.approved:
        p.status = ProposalStatus.REJECTED
        p.risk_note = f"blocked at execution: {rd.reason}"
        return False, rd.reason

    snap = rt.last_market_snaps.get(p.market_ticker)
    result, nf = await place_and_book(
        rt,
        coid=f"appr:{proposal_id}"[:60],
        match_id=p.match_id,
        market_ticker=p.market_ticker,
        outcome=p.outcome,
        action=p.action,
        contracts=rd.contracts,
        limit_price_cents=p.limit_price_cents,
        cost_per_contract=p.cost_per_contract,
        snap=snap,
        persist=persist,
        minute=p.minute,
    )
    if result.is_filled:
        p.status = ProposalStatus.EXECUTED
        p.result = {
            "filled": result.filled_contracts,
            "avg_price_cents": result.avg_price_cents,
            "fee": result.fee,
        }
        if persist:
            rt.audit.log("approved", f"approved & executed {proposal_id}", match_id=p.match_id, proposal_id=proposal_id)
        return True, "executed"
    p.status = ProposalStatus.FAILED
    p.result = {"status": result.status.value, "message": result.message}
    return False, result.message or "order not filled"


def reject_proposal(rt: "Runtime", proposal_id: str) -> bool:
    p: TradeProposal | None = rt.proposals.get(proposal_id)
    if p is None or not p.is_pending:
        return False
    p.status = ProposalStatus.REJECTED
    p.risk_note = "rejected by user"
    rt.state.add_decision(
        {
            "kind": "reject",
            "match_id": p.match_id,
            "message": f"REJECTED {p.action.value} {p.contracts} {p.market_ticker}",
        }
    )
    return True


def expire_proposals(rt: "Runtime", now=None) -> None:
    now = now or utcnow()
    for p in rt.proposals.values():
        if p.is_pending and p.expires_ts is not None and p.expires_ts < now:
            p.status = ProposalStatus.EXPIRED


def _closing_price_cents(snap: MarketSnapshot | None, action: OrderAction) -> int:
    """Marketable price to flatten: cross the spread to guarantee the close fills."""
    if snap is not None:
        if action is OrderAction.BUY and snap.yes_ask:
            return int(snap.yes_ask)
        if action is OrderAction.SELL and snap.yes_bid:
            return int(snap.yes_bid)
        mid = snap.yes_mid_cents
        if mid:
            return int(round(mid))
    # No quote: assume the worst marketable price so we still flatten.
    return 99 if action is OrderAction.BUY else 1


async def flatten_all(rt: "Runtime", *, reason: str = "kill switch flatten", persist: bool = True) -> int:
    """Place closing orders for every open position (kill-switch flatten).

    Bypasses the pre-trade risk gate on purpose (the kill switch has engaged and we
    must reduce, not block) but still books the resulting fills. Returns the number of
    markets flattened.
    """
    flattened = 0
    for ticker, pos in list(rt.portfolio.positions.items()):
        net = pos.net_yes
        if net == 0:
            continue
        action = OrderAction.SELL if net > 0 else OrderAction.BUY
        snap = rt.last_market_snaps.get(ticker)
        price = _closing_price_cents(snap, action)
        contracts = abs(net)
        coid = f"flat:{ticker}:{int(utcnow().timestamp())}"[:60]
        _result, n_fills = await place_and_book(
            rt,
            coid=coid,
            match_id=pos.match_id,
            market_ticker=ticker,
            outcome=pos.outcome,
            action=action,
            contracts=contracts,
            limit_price_cents=price,
            cost_per_contract=(price if action is OrderAction.BUY else 100 - price) / 100.0,
            snap=snap,
            persist=persist,
        )
        if n_fills:
            flattened += 1
    if flattened:
        rt.audit.guardrail(f"{reason}: flattened {flattened} markets")
        rt.bus.publish(Event(EventType.ALERT, {"kind": "flatten", "message": f"{reason}: {flattened} markets"}, None))
    return flattened


async def sweep_resting_orders(rt: "Runtime", *, timeout_seconds: float, now=None) -> int:
    """Cancel resting (unfilled) orders older than ``timeout_seconds`` (live order
    lifecycle: don't leave stale limit orders exposed to adverse selection)."""
    now = now or utcnow()
    canceled = 0
    for oid, info in list(rt.resting_orders.items()):
        age = (now - info["placed_ts"]).total_seconds()
        if age < timeout_seconds:
            continue
        ok = await rt.executor.cancel(oid)
        rt.resting_orders.pop(oid, None)
        if ok:
            canceled += 1
            rt.audit.log("cancel", f"resting order timed out after {age:.0f}s", coid=info.get("coid"))
    return canceled


def proposals_view(rt: "Runtime") -> dict[str, Any]:
    pending = sorted(
        (p for p in rt.proposals.values() if p.is_pending),
        key=lambda p: abs(p.net_edge),
        reverse=True,
    )
    decided = sorted(
        (p for p in rt.proposals.values() if not p.is_pending),
        key=lambda p: p.ts,
        reverse=True,
    )[:12]
    return {
        "pending": [p.model_dump(mode="json") for p in pending],
        "recent": [p.model_dump(mode="json") for p in decided],
    }


# --- persistence helpers (moved here so both paths share them) ------------- #
def _persist_order(rt: "Runtime", order, result) -> None:
    from ..models.db import OrderRow

    with rt.db.session() as s:
        s.add(
            OrderRow(
                client_order_id=order.client_order_id,
                exchange_order_id=result.exchange_order_id,
                match_id=order.match_id,
                market_ticker=order.market_ticker,
                ts=utcnow(),
                action=order.action.value,
                side=order.side.value,
                count=order.contracts,
                price_cents=order.limit_price_cents,
                status=result.status.value,
                mode=rt.executor.mode,
                data={"reason": result.message, "filled": result.filled_contracts},
            )
        )


def _persist_fill(rt: "Runtime", fill) -> None:
    from ..models.db import FillRow

    with rt.db.session() as s:
        s.add(
            FillRow(
                client_order_id=fill.client_order_id,
                match_id=fill.match_id,
                market_ticker=fill.market_ticker,
                ts=utcnow(),
                action=fill.action.value,
                side="yes",
                count=fill.contracts,
                price_cents=fill.price_cents,
                fee=fill.fee,
                data={},
            )
        )
