"""Capture the BROADER per-match Kalshi market set (beyond 1X2) for later modelling.

The live strategy trades only `KXWCGAME` (1X2). To research/validate the other per-match
markets (Total, Spread, BTTS, Correct Score, Team Total, 1st/2nd-half family, corners,
shots, SOG, saves — see docs/markets_roadmap.md Tiers 1/2/4) we need their PRICES recorded
alongside the match xG we already capture. This module pulls them into the generic
``raw_market_quotes`` table.

Efficiency: every per-match WC series shares the same event suffix as KXWCGAME
(`KXWCGAME-26JUN27JORARG` → `KXWCTOTAL-26JUN27JORARG`), so one `get_markets(event_ticker=…)`
call per series fetches all of that fixture's markets for it — no per-series team matching.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ...logging_setup import get_logger
from ...util import utcnow
from .feed import _price_cents

log = get_logger("kalshi.extra_markets")

# Per-match WC series worth recording (roadmap Tiers 1/2/4). KXWCGAME is already captured
# by the typed market feed, so it's omitted here.
EXTRA_WC_SERIES: tuple[str, ...] = (
    # Tier 1 — full-match scoreline-derived
    "KXWCTOTAL", "KXWCSPREAD", "KXWCBTTS", "KXWCSCORE", "KXWCTEAMTOTAL", "KXWCWINMARGIN",
    "KXWCFTTS", "KXWCTTSF",
    # Tier 2 — per half
    "KXWC1H", "KXWC2H", "KXWC1HTOTAL", "KXWC2HTOTAL", "KXWC1HSPREAD", "KXWC2HSPREAD",
    "KXWC1HBTTS", "KXWC2HBTTS", "KXWC1HSCORE",
    # Tier 4 — live non-goal stats
    "KXWCCORNERS", "KXWCTCORNERS", "KXWCSHOT", "KXWCTEAMSHOT", "KXWCSOG", "KXWCTEAMSOG",
    "KXWCSAVE",
)


def quote_row(match_id: str, series: str, market: dict[str, Any], ts: datetime) -> dict[str, Any]:
    """Map one Kalshi market object to a RawMarketQuoteRow kwargs dict."""
    return {
        "match_id": match_id,
        "series": series,
        "market_ticker": str(market.get("ticker", "")),
        "ts": ts,
        "yes_sub_title": market.get("yes_sub_title") or market.get("subtitle"),
        "floor_strike": market.get("floor_strike"),
        "strike_type": market.get("strike_type"),
        "yes_bid": _price_cents(market, "yes_bid_dollars", "yes_bid"),
        "yes_ask": _price_cents(market, "yes_ask_dollars", "yes_ask"),
        "last_price": _price_cents(market, "last_price_dollars", "last_price"),
        "status": str(market.get("status", "")),
        "data": market,
    }


async def capture_extra_markets(
    client: Any,
    match_id: str,
    event_suffix: str,
    *,
    series: tuple[str, ...] = EXTRA_WC_SERIES,
) -> list[dict[str, Any]]:
    """Fetch every configured per-match series for one fixture and return quote rows.

    ``event_suffix`` is the match part of the KXWCGAME event ticker (e.g. ``26JUN27JORARG``).
    One ``get_markets`` call per series; missing series are skipped quietly.
    """
    ts = utcnow()
    rows: list[dict[str, Any]] = []
    for s in series:
        try:
            payload = await client.get_markets(event_ticker=f"{s}-{event_suffix}")
        except Exception as exc:  # one bad series must not stall the capture
            log.debug("extra-market fetch failed", extra={"series": s, "err": str(exc)})
            continue
        for m in payload.get("markets", []) or []:
            rows.append(quote_row(match_id, s, m, ts))
    return rows
