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


def implied_from_markets(
    snapshots: list[MarketSnapshot],
    *,
    method: str = "proportional",
    match_id: str | None = None,
) -> MarketView:
    """De-vig a set of 1X2 Yes markets into implied probabilities."""
    by_outcome = {s.outcome: s for s in snapshots}
    present = [o for o in (Outcome.HOME, Outcome.DRAW, Outcome.AWAY) if o in by_outcome]
    mids = [by_outcome[o].yes_mid_prob for o in present]
    valid = [(o, m) for o, m in zip(present, mids) if m is not None]

    devig = _METHODS.get(method, _devig_proportional)
    raw = [m for _, m in valid]
    implied = devig(raw) if raw else []
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
        match_id=mid, ts=ts, overround=overround, method=method, outcomes=outcomes
    )
