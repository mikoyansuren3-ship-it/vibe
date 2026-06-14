"""A simulated Kalshi market for paper mode.

Generates ``MarketSnapshot``s (home/draw/away Yes markets) for a live match so the
edge/sizing/execution path runs with no exchange. Crucially the market's view is
*naive* — it reacts to score, time, and Elo but **ignores live xG** — and carries
an overround (vig) plus a bid/ask spread and autocorrelated noise. That makes it a
realistic, imperfect counterparty: when live xG diverges from the scoreline, our
xG-aware model disagrees with this market and a genuine edge appears.
"""

from __future__ import annotations

import math
import random

from ...models.schemas import BookLevel, MarketSnapshot, MatchSnapshot, Outcome
from ...util import clamp


def _poisson_pmf(lam: float, k: int) -> float:
    if lam <= 0:
        return 1.0 if k == 0 else 0.0
    return math.exp(-lam) * lam**k / math.factorial(k)


def naive_market_probs(match: MatchSnapshot, max_goals: int = 10) -> tuple[float, float, float]:
    """Score/time/Elo-only 1X2 estimate (no xG) — the market's blind spot."""
    ctx = match.context
    home_elo = (ctx.home_elo if ctx else None) or 1750.0
    away_elo = (ctx.away_elo if ctx else None) or 1750.0
    diff = (home_elo - away_elo) / 400.0
    total = 2.6
    hshare = 1.0 / (1.0 + 10 ** (-diff)) * 0.5 + 0.25
    rem = match.minutes_remaining / 90.0
    lam_h = max(0.01, total * hshare * rem)
    lam_a = max(0.01, total * (1.0 - hshare) * rem)

    # Crude red-card nudge (less reactive than the real model).
    if match.net_red_cards > 0:
        lam_h *= 1.1
        lam_a *= 0.9
    elif match.net_red_cards < 0:
        lam_h *= 0.9
        lam_a *= 1.1

    base_diff = match.score_diff
    p_home = p_draw = p_away = 0.0
    for gh in range(max_goals + 1):
        ph = _poisson_pmf(lam_h, gh)
        for ga in range(max_goals + 1):
            p = ph * _poisson_pmf(lam_a, ga)
            final = base_diff + gh - ga
            if final > 0:
                p_home += p
            elif final == 0:
                p_draw += p
            else:
                p_away += p
    total_p = p_home + p_draw + p_away or 1.0
    return p_home / total_p, p_draw / total_p, p_away / total_p


class SimulatedMarket:
    def __init__(
        self,
        match_id: str,
        *,
        seed: int = 7,
        overround: float = 1.05,
        spread_cents: int = 3,
        noise_sd: float = 0.015,
    ) -> None:
        self.match_id = match_id
        self.rng = random.Random(hash((match_id, seed)) & 0xFFFFFFFF)
        self.overround = overround
        self.spread_cents = spread_cents
        self.noise_sd = noise_sd
        self.event_ticker = f"KXWC-{match_id.upper()}"
        self.tickers = {
            Outcome.HOME: f"{self.event_ticker}-H",
            Outcome.DRAW: f"{self.event_ticker}-D",
            Outcome.AWAY: f"{self.event_ticker}-A",
        }
        self._noise = {Outcome.HOME: 0.0, Outcome.DRAW: 0.0, Outcome.AWAY: 0.0}
        self._volume = {o: 0 for o in self.tickers}

    def _step_noise(self) -> None:
        for o in self._noise:
            # autocorrelated random walk, mean-reverting
            self._noise[o] = self._noise[o] * 0.8 + self.rng.gauss(0, self.noise_sd)

    def snapshots(self, match: MatchSnapshot) -> list[MarketSnapshot]:
        probs = dict(zip(Outcome, naive_market_probs(match)))
        self._step_noise()
        out: list[MarketSnapshot] = []
        for outcome, ticker in self.tickers.items():
            p = clamp(probs[outcome] + self._noise[outcome], 0.01, 0.99)
            mid = clamp(p * self.overround * 100.0, 1.0, 99.0)
            half = self.spread_cents / 2.0
            yes_bid = int(clamp(round(mid - half), 1, 98))
            yes_ask = int(clamp(round(mid + half), yes_bid + 1, 99))
            last = int(clamp(round(mid + self.rng.gauss(0, 0.5)), 1, 99))
            self._volume[outcome] += self.rng.randint(0, 40)
            out.append(
                MarketSnapshot(
                    market_ticker=ticker,
                    event_ticker=self.event_ticker,
                    match_id=self.match_id,
                    outcome=outcome,
                    yes_bid=yes_bid,
                    yes_ask=yes_ask,
                    last_price=last,
                    volume=self._volume[outcome],
                    open_interest=self._volume[outcome] * 3,
                    yes_depth=[
                        BookLevel(price_cents=yes_bid, size=self.rng.randint(20, 200)),
                        BookLevel(
                            price_cents=int(clamp(yes_bid - 1, 1, 98)),
                            size=self.rng.randint(50, 400),
                        ),
                    ],
                    no_depth=[
                        BookLevel(
                            price_cents=int(clamp(100 - yes_ask, 1, 98)),
                            size=self.rng.randint(20, 200),
                        ),
                    ],
                    status="active" if not match.period.is_finished else "closed",
                    settlement_rule=(
                        f"Resolves YES if {outcome.value} is the full-time result of "
                        f"{match.home_team} vs {match.away_team} (90 min, draw included)."
                    ),
                )
            )
        return out
