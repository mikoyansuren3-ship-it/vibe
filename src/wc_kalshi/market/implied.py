"""Convert Kalshi market prices into de-vigged implied probabilities.

A Yes mid-price ≈ market-implied probability *including* the overround (the three
1X2 Yes prices sum to >100%). To compare apples-to-apples with the model we strip
the overround. Three methods are provided:

  * ``proportional`` — divide each raw prob by their sum (fast, standard default).
  * ``power``        — p_i ∝ q_i**(1/k), solve k so probabilities sum to 1
                       (handles favourite-longshot bias).
  * ``shin``         — Shin (1992) insider-trading model, solved numerically.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from ..models.schemas import MarketSnapshot, Outcome, Probabilities
from ..util import utcnow


@dataclass
class OutcomeMarket:
    outcome: Outcome
    market_ticker: str
    yes_bid: int | None
    yes_ask: int | None
    mid_prob: float | None  # raw mid, includes vig
    implied_prob: float  # de-vigged


@dataclass
class MarketView:
    match_id: str
    ts: datetime
    overround: float
    method: str
    outcomes: dict[Outcome, OutcomeMarket]
    # True only when the FULL 1X2 book was de-vigged (all three legs two-sided).
    # De-vig over a partial book is incoherent: proportional normalization forces
    # 2 legs to sum to 1.0, inflating both — so an incomplete view keeps RAW mids
    # in ``implied_prob`` and the edge detector must not act on it.
    complete: bool = True

    def probabilities(self) -> Probabilities:
        return Probabilities(
            match_id=self.match_id,
            ts=self.ts,
            p_home=self.outcomes[Outcome.HOME].implied_prob if Outcome.HOME in self.outcomes else 0.0,
            p_draw=self.outcomes[Outcome.DRAW].implied_prob if Outcome.DRAW in self.outcomes else 0.0,
            p_away=self.outcomes[Outcome.AWAY].implied_prob if Outcome.AWAY in self.outcomes else 0.0,
            source="market",
            meta={"overround": round(self.overround, 4), "method": self.method},
        )


def _devig_proportional(q: list[float]) -> list[float]:
    total = sum(q)
    return [x / total for x in q] if total > 0 else q


def _devig_power(q: list[float]) -> list[float]:
    """p_i = q_i**(1/k) with k chosen so the powered gross prices sum to 1.

    For q_i in (0,1), s(k)=Σ q_i**(1/k) is increasing in k (k->0 => sum->0,
    k->inf => sum->n), so we bisect for s(k)=1.
    """
    total = sum(q)
    if total <= 0:
        return q

    def s(k: float) -> float:
        return sum(x ** (1.0 / k) for x in q)

    lo, hi = 1e-3, 10.0
    for _ in range(80):
        mid = (lo + hi) / 2
        if s(mid) > 1.0:
            hi = mid
        else:
            lo = mid
    k = (lo + hi) / 2
    vals = [x ** (1.0 / k) for x in q]
    z = sum(vals)
    return [v / z for v in vals] if z > 0 else _devig_proportional(q)


def _devig_shin(q: list[float]) -> list[float]:
    total = sum(q)
    if total <= 0:
        return q
    norm = [x / total for x in q]

    def shin_probs(z: float) -> list[float]:
        return [
            ((z * z + 4 * (1 - z) * (pi * pi) / total) ** 0.5 - z) / (2 * (1 - z))
            if z < 1
            else pi
            for pi in q
        ]

    lo, hi = 0.0, 0.2
    for _ in range(60):
        z = (lo + hi) / 2
        p = shin_probs(z)
        if sum(p) > 1.0:
            lo = z
        else:
            hi = z
    p = shin_probs((lo + hi) / 2)
    s = sum(p)
    return [pi / s for pi in p] if s > 0 else norm


_METHODS = {
    "proportional": _devig_proportional,
    "power": _devig_power,
    "shin": _devig_shin,
}


def implied_two_way(yes_bid: int | None, yes_ask: int | None) -> tuple[float | None, float]:
    """Fair probability + exact overround for a BINARY (Yes/No) market from its two-sided
    book — for derived O/U, BTTS, and knockout "to advance" contracts (plan P0.3).

    The No book mirrors the Yes book (``no_ask = 100 − yes_bid``), so the Yes mid is already
    the fair probability — the overround lives entirely in the SPREAD (the cost of crossing),
    not in the mid. The tradeable pair (``yes_ask`` to back, ``no_ask = 100 − yes_bid`` to lay)
    sums to ``100 + spread``, i.e. overround ``= 1 + spread/100``; a Yes-only mid would
    understate it. Returns ``(fair_prob, overround)``; ``fair_prob`` is None for a one-sided book.
    """
    if yes_bid is None or yes_ask is None:
        return None, 1.0
    fair = (yes_bid + yes_ask) / 200.0
    overround = 1.0 + (yes_ask - yes_bid) / 100.0
    return fair, overround


def implied_from_markets(
    snapshots: list[MarketSnapshot],
    *,
    method: str = "proportional",
    match_id: str | None = None,
) -> MarketView:
    """De-vig a set of 1X2 Yes markets into implied probabilities.

    Inputs are the strict two-sided book mids (``yes_book_mid_prob``) — a leg whose
    book is one-sided contributes nothing, even if it has a ``last_price``. De-vig
    runs only on a COMPLETE three-leg book; a partial book is returned with raw mids
    and ``complete=False`` so downstream consumers don't act on inflated numbers.
    """
    by_outcome = {s.outcome: s for s in snapshots}
    present = [o for o in (Outcome.HOME, Outcome.DRAW, Outcome.AWAY) if o in by_outcome]
    mids = [by_outcome[o].yes_book_mid_prob for o in present]
    valid = [(o, m) for o, m in zip(present, mids) if m is not None]
    complete = len(valid) == 3

    devig = _METHODS.get(method, _devig_proportional)
    raw = [m for _, m in valid]
    implied = devig(raw) if complete else raw
    overround = sum(raw) if raw else 0.0

    outcomes: dict[Outcome, OutcomeMarket] = {}
    impl_iter = iter(implied)
    for o, m in valid:
        snap = by_outcome[o]
        outcomes[o] = OutcomeMarket(
            outcome=o,
            market_ticker=snap.market_ticker,
            yes_bid=snap.yes_bid,
            yes_ask=snap.yes_ask,
            mid_prob=m,
            implied_prob=next(impl_iter),
        )

    mid = match_id or (snapshots[0].match_id if snapshots else "")
    ts = snapshots[0].ts if snapshots else utcnow()
    return MarketView(
        match_id=mid, ts=ts, overround=overround, method=method, outcomes=outcomes,
        complete=complete,
    )
