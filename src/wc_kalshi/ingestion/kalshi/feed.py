"""Market feed: turn a live match into normalized ``MarketSnapshot``s.

Two implementations behind one interface:
  * ``SimulatedMarketFeed`` — paper mode, no exchange (default).
  * ``LiveKalshiMarketFeed`` — demo/live, hits the Kalshi REST API and discovers
    the match's market tickers at runtime.

Orderbook/market parsing is isolated in pure functions for offline testing.
"""

from __future__ import annotations

import time
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
    def __init__(self, *, seed: int = 7, xg_awareness: float = 0.0, model=None) -> None:
        self.seed = seed
        self.xg_awareness = xg_awareness
        self.model = model
        self._markets: dict[str, SimulatedMarket] = {}

    def _market(self, match_id: str) -> SimulatedMarket:
        if match_id not in self._markets:
            self._markets[match_id] = SimulatedMarket(
                match_id, seed=self.seed, xg_awareness=self.xg_awareness, model=self.model
            )
        return self._markets[match_id]

    async def snapshots_for_match(self, match: MatchSnapshot) -> list[MarketSnapshot]:
        return self._market(match.match_id).snapshots(match)


# --------------------------------------------------------------------------- #
# Live parsing helpers (pure, testable)
# --------------------------------------------------------------------------- #
def _to_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None


def _price_cents(obj: dict[str, Any], dollars_key: str, cents_key: str) -> int | None:
    """Price in integer cents from a Kalshi market object: prefer the dollar field
    ("0.2900" -> 29), fall back to the legacy cent field (29)."""
    dv = obj.get(dollars_key)
    if dv not in (None, ""):
        try:
            return round(float(dv) * 100)
        except (TypeError, ValueError):
            pass
    return _to_int(obj.get(cents_key))


def parse_orderbook(
    ob_json: dict[str, Any],
) -> tuple[int | None, int | None, list[BookLevel], list[BookLevel]]:
    """Return (yes_bid, yes_ask, yes_depth, no_depth) from a Kalshi orderbook.

    Best Yes bid = max Yes price; best Yes ask = 100 - max No price. Handles both the
    current dollar/fixed-point schema (``orderbook_fp`` with ``yes_dollars``/``no_dollars``
    levels as ``["0.8100", "6357.11"]``) and the legacy cents schema (``orderbook`` with
    ``yes``/``no`` levels as ``[price_cents, size]``).
    """
    ob = ob_json.get("orderbook_fp") or ob_json.get("orderbook") or ob_json or {}
    yes_levels = ob.get("yes_dollars")
    no_levels = ob.get("no_dollars")
    is_dollars = yes_levels is not None or no_levels is not None
    if not is_dollars:  # legacy cents schema
        yes_levels = ob.get("yes")
        no_levels = ob.get("no")
    yes_levels = yes_levels or []
    no_levels = no_levels or []

    def to_levels(raw: list[Any]) -> list[BookLevel]:
        out: list[BookLevel] = []
        for level in raw:
            if isinstance(level, (list, tuple)) and len(level) >= 2:
                price = float(level[0])
                cents = round(price * 100) if is_dollars else int(round(price))
                if cents > 0:
                    out.append(BookLevel(price_cents=cents, size=int(round(float(level[1])))))
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
    """Build a ``MarketSnapshot`` from a Kalshi market object (+ optional orderbook).

    Reads the current dollar fields (``yes_bid_dollars`` = "0.2900") and falls back to the
    legacy cent fields (``yes_bid`` = 29) so captured payloads of either era parse.
    """
    yes_bid = _price_cents(market_obj, "yes_bid_dollars", "yes_bid")
    yes_ask = _price_cents(market_obj, "yes_ask_dollars", "yes_ask")
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
        last_price=_price_cents(market_obj, "last_price_dollars", "last_price"),
        volume=int(float(market_obj.get("volume_fp") or market_obj.get("volume") or 0)),
        open_interest=_to_int(market_obj.get("open_interest_fp") or market_obj.get("open_interest")),
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
        map_retry_seconds: float = 60.0,
    ) -> None:
        self.client = client
        self.series_ticker = series_ticker
        self.fetch_depth = fetch_depth
        # Successful mappings are cached for the match's lifetime. Failures are NOT:
        # a transient 5xx (or an event that hasn't flipped to "open" yet) at first tick
        # must not silently disable the match's market feed for the whole game — retry
        # after a backoff instead.
        self.map_retry_seconds = map_retry_seconds
        self._maps: dict[str, MatchMarketMap] = {}
        self._map_retry_at: dict[str, float] = {}  # match_id -> monotonic next-attempt time

    async def _map_for(self, match: MatchSnapshot) -> MatchMarketMap | None:
        mapping = self._maps.get(match.match_id)
        if mapping is not None:
            return mapping
        now = time.monotonic()
        if now < self._map_retry_at.get(match.match_id, 0.0):
            return None  # recent failure — wait out the backoff before re-resolving
        try:
            mapping = await resolve_market_map(
                self.client,
                match.match_id,
                match.home_team,
                match.away_team,
                series_ticker=self.series_ticker,
            )
        except Exception as exc:
            log.warning("market map resolve failed", extra={"err": str(exc)})
            mapping = None
        if mapping is None:
            self._map_retry_at[match.match_id] = now + self.map_retry_seconds
            log.warning(
                "no Kalshi market mapped; will retry",
                extra={
                    "match_id": match.match_id,
                    "teams": f"{match.home_team}-{match.away_team}",
                    "retry_in_s": self.map_retry_seconds,
                },
            )
            return None
        self._maps[match.match_id] = mapping
        self._map_retry_at.pop(match.match_id, None)
        return mapping

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
        from ...modeling.base import build_model

        return SimulatedMarketFeed(
            seed=cfg.football.sim_seed,
            xg_awareness=cfg.football.sim_market_xg_awareness,
            model=build_model(cfg) if cfg.football.sim_market_xg_awareness > 0 else None,
        )

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
