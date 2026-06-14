"""Market feed: turn a live match into normalized ``MarketSnapshot``s.

Two implementations behind one interface:
  * ``SimulatedMarketFeed`` — paper mode, no exchange (default).
  * ``LiveKalshiMarketFeed`` — demo/live, hits the Kalshi REST API and discovers
    the match's market tickers at runtime.

Orderbook/market parsing is isolated in pure functions for offline testing.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

from ...logging_setup import get_logger
from ...models.schemas import BookLevel, MarketSnapshot, MatchSnapshot, Outcome
from .market_map import MatchMarketMap, resolve_market_map
from .sim_market import SimulatedMarket

if TYPE_CHECKING:
    from ...config import AppConfig
    from .client import KalshiClient

log = get_logger("kalshi.feed")


class MarketFeed(ABC):
    @abstractmethod
    async def snapshots_for_match(self, match: MatchSnapshot) -> list[MarketSnapshot]:
        ...

    async def aclose(self) -> None:  # pragma: no cover - default
        return None


class SimulatedMarketFeed(MarketFeed):
    def __init__(self, *, seed: int = 7) -> None:
        self.seed = seed
        self._markets: dict[str, SimulatedMarket] = {}

    def _market(self, match_id: str) -> SimulatedMarket:
        if match_id not in self._markets:
            self._markets[match_id] = SimulatedMarket(match_id, seed=self.seed)
        return self._markets[match_id]

    async def snapshots_for_match(self, match: MatchSnapshot) -> list[MarketSnapshot]:
        return self._market(match.match_id).snapshots(match)


# --------------------------------------------------------------------------- #
# Live parsing helpers (pure, testable)
# --------------------------------------------------------------------------- #
def parse_orderbook(
    ob_json: dict[str, Any],
) -> tuple[int | None, int | None, list[BookLevel], list[BookLevel]]:
    """Return (yes_bid, yes_ask, yes_depth, no_depth) from a Kalshi orderbook.

    Kalshi orderbooks list ``yes`` and ``no`` bid levels as ``[price_cents, size]``.
    Best Yes bid = max Yes price; best Yes ask = 100 - max No price.
    """
    ob = ob_json.get("orderbook", ob_json) or {}
    yes_levels = ob.get("yes") or []
    no_levels = ob.get("no") or []

    def to_levels(raw: list[Any]) -> list[BookLevel]:
        out: list[BookLevel] = []
        for level in raw:
            if isinstance(level, (list, tuple)) and len(level) >= 2:
                out.append(BookLevel(price_cents=int(level[0]), size=int(level[1])))
        return out

    yes_depth = sorted(to_levels(yes_levels), key=lambda b: b.price_cents, reverse=True)
    no_depth = sorted(to_levels(no_levels), key=lambda b: b.price_cents, reverse=True)
    yes_bid = yes_depth[0].price_cents if yes_depth else None
    yes_ask = (100 - no_depth[0].price_cents) if no_depth else None
    return yes_bid, yes_ask, yes_depth, no_depth


def market_snapshot_from_api(
    match_id: str,
    outcome: Outcome,
    market_obj: dict[str, Any],
    ob_json: dict[str, Any] | None = None,
) -> MarketSnapshot:
    """Build a ``MarketSnapshot`` from a Kalshi market object (+ optional orderbook)."""
    yes_bid = market_obj.get("yes_bid")
    yes_ask = market_obj.get("yes_ask")
    yes_depth: list[BookLevel] = []
    no_depth: list[BookLevel] = []
    if ob_json is not None:
        ob_bid, ob_ask, yes_depth, no_depth = parse_orderbook(ob_json)
        yes_bid = yes_bid if yes_bid not in (None, 0) else ob_bid
        yes_ask = yes_ask if yes_ask not in (None, 0) else ob_ask
    return MarketSnapshot(
        market_ticker=str(market_obj.get("ticker", "")),
        event_ticker=market_obj.get("event_ticker"),
        match_id=match_id,
        outcome=outcome,
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        last_price=market_obj.get("last_price"),
        volume=int(market_obj.get("volume") or 0),
        open_interest=market_obj.get("open_interest"),
        yes_depth=yes_depth,
        no_depth=no_depth,
        status=str(market_obj.get("status", "active")),
        settlement_rule=market_obj.get("rules_primary") or market_obj.get("subtitle"),
    )


class LiveKalshiMarketFeed(MarketFeed):
    def __init__(
        self,
        client: "KalshiClient",
        *,
        series_ticker: str | None = None,
        fetch_depth: bool = True,
    ) -> None:
        self.client = client
        self.series_ticker = series_ticker
        self.fetch_depth = fetch_depth
        self._maps: dict[str, MatchMarketMap | None] = {}

    async def _map_for(self, match: MatchSnapshot) -> MatchMarketMap | None:
        if match.match_id not in self._maps:
            try:
                self._maps[match.match_id] = await resolve_market_map(
                    self.client,
                    match.match_id,
                    match.home_team,
                    match.away_team,
                    series_ticker=self.series_ticker,
                )
            except Exception as exc:
                log.warning("market map resolve failed", extra={"err": str(exc)})
                self._maps[match.match_id] = None
            if self._maps[match.match_id] is None:
                log.warning(
                    "no Kalshi market mapped",
                    extra={"match_id": match.match_id, "teams": f"{match.home_team}-{match.away_team}"},
                )
        return self._maps[match.match_id]

    async def snapshots_for_match(self, match: MatchSnapshot) -> list[MarketSnapshot]:
        mapping = await self._map_for(match)
        if mapping is None:
            return []
        snapshots: list[MarketSnapshot] = []
        for outcome, ticker in mapping.outcomes():
            try:
                market_obj = (await self.client.get_market(ticker)).get("market", {})
                ob = None
                if self.fetch_depth:
                    ob = await self.client.get_orderbook(ticker)
                snapshots.append(
                    market_snapshot_from_api(match.match_id, outcome, market_obj, ob)
                )
            except Exception as exc:
                log.warning("market fetch failed", extra={"ticker": ticker, "err": str(exc)})
        return snapshots

    async def aclose(self) -> None:
        await self.client.aclose()


def build_market_feed(cfg: "AppConfig") -> MarketFeed:
    """Paper -> simulated; demo/live -> live Kalshi REST feed."""
    if cfg.is_paper:
        return SimulatedMarketFeed(seed=cfg.football.sim_seed)

    from .auth import KalshiSigner
    from .client import KalshiClient

    signer = None
    if cfg.secrets.has_kalshi_creds():
        signer = KalshiSigner(cfg.secrets.kalshi_api_key_id, cfg.secrets.kalshi_private_key())
    else:
        raise ValueError(
            f"mode={cfg.mode.value} requires Kalshi credentials "
            "(KALSHI_API_KEY_ID + KALSHI_PRIVATE_KEY_PATH/PEM)."
        )
    client = KalshiClient(
        cfg.kalshi_rest_base,
        signer=signer,
        timeout=cfg.kalshi.request_timeout_seconds,
        max_retries=cfg.kalshi.max_retries,
    )
    return LiveKalshiMarketFeed(client, series_ticker=cfg.kalshi.worldcup_series_ticker)
