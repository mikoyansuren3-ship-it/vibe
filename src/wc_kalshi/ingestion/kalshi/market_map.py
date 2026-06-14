"""Resolve a live match to its Kalshi market tickers.

Kalshi's exact World Cup tickers aren't authoritatively published (research.md
§1.7), so we DISCOVER the mapping at runtime instead of hard-coding it: pull events
for the configured series, fuzzy-match an event's title to the two team names, then
classify each nested market as home / draw / away by its Yes label.

The matcher is a pure function tested offline against a captured ``/events`` payload.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from typing import Any

from ...models.schemas import Outcome


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    return "".join(c.lower() if c.isalnum() else " " for c in text)


_ALIASES = {
    "usa": {"usa", "united states", "us", "americans"},
    "south korea": {"south korea", "korea republic", "korea"},
    "netherlands": {"netherlands", "holland"},
}


def _team_tokens(team: str) -> set[str]:
    base = {_norm(team).strip()}
    base |= {_norm(a).strip() for a in _ALIASES.get(_norm(team).strip(), set())}
    return {t for t in base if t}


def _text_mentions_team(text_norm: str, team: str) -> bool:
    for token in _team_tokens(team):
        if token and token in text_norm:
            return True
    # token overlap fallback for multi-word names
    words = {w for w in _norm(team).split() if len(w) > 3}
    return any(w in text_norm for w in words)


@dataclass
class MatchMarketMap:
    match_id: str
    event_ticker: str
    home_team: str
    away_team: str
    tickers: dict[Outcome, str] = field(default_factory=dict)

    def outcomes(self) -> list[tuple[Outcome, str]]:
        return list(self.tickers.items())


def _classify_market(market: dict[str, Any], home_team: str, away_team: str) -> Outcome | None:
    # Classify on the human-readable label ONLY. The ticker concatenates both team
    # abbreviations (e.g. "...USAWAL-WAL"), so matching team names against it gives
    # false positives ("usa" is a substring of "usawal").
    label = _norm(
        " ".join(
            str(market.get(k, ""))
            for k in ("yes_sub_title", "yes_subtitle", "subtitle", "title")
        )
    )
    if any(w in label for w in ("draw", "tie", "no winner")):
        return Outcome.DRAW
    if _text_mentions_team(label, home_team):
        return Outcome.HOME
    if _text_mentions_team(label, away_team):
        return Outcome.AWAY
    return None


def match_event_to_markets(
    events_payload: dict[str, Any],
    match_id: str,
    home_team: str,
    away_team: str,
) -> MatchMarketMap | None:
    """Find the event whose title mentions both teams and map its markets."""
    for event in events_payload.get("events", []):
        text = _norm(
            " ".join(str(event.get(k, "")) for k in ("title", "sub_title", "subtitle"))
        )
        if not (_text_mentions_team(text, home_team) and _text_mentions_team(text, away_team)):
            continue
        mapping = MatchMarketMap(
            match_id=match_id,
            event_ticker=str(event.get("event_ticker", "")),
            home_team=home_team,
            away_team=away_team,
        )
        for market in event.get("markets", []):
            outcome = _classify_market(market, home_team, away_team)
            ticker = market.get("ticker")
            if outcome is not None and ticker and outcome not in mapping.tickers:
                mapping.tickers[outcome] = ticker
        if mapping.tickers:
            return mapping
    return None


async def resolve_market_map(
    client: Any,
    match_id: str,
    home_team: str,
    away_team: str,
    *,
    series_ticker: str | None = None,
) -> MatchMarketMap | None:
    """Network wrapper: fetch events for the series and resolve the mapping."""
    payload = await client.get_events(
        series_ticker=series_ticker, status="open", with_nested_markets=True
    )
    return match_event_to_markets(payload, match_id, home_team, away_team)
