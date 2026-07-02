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


def _phrase_in(phrase: str, text_norm: str) -> bool:
    """Whole-word/whole-phrase containment. Raw substring matching is unsafe here:
    the "us" alias is a substring of "aUStralia"/"rUSsia", so a USA fixture could
    bind to (or classify as) a completely different country's market."""
    return f" {phrase} " in f" {' '.join(text_norm.split())} "


def _team_match_score(text_norm: str, team: str) -> int:
    """0 if the text does not mention the team; otherwise the length of the longest
    matched name/alias. The length makes the score a specificity rank: "south korea"
    (11) outranks the bare "korea" alias (5), which also matches "North Korea"."""
    best = 0
    for token in _team_tokens(team):
        if _phrase_in(token, text_norm):
            best = max(best, len(token))
    # single-word fallback for multi-word names ("Republic of Ireland" -> "ireland")
    for w in _norm(team).split():
        if len(w) > 3 and _phrase_in(w, text_norm):
            best = max(best, len(w))
    return best


def _text_mentions_team(text_norm: str, team: str) -> bool:
    return _team_match_score(text_norm, team) > 0


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
    # Classify on the YES-SPECIFIC label only. Two traps to avoid:
    #  * the ticker concatenates both team abbreviations ("...USAWAL-WAL"), and
    #  * the market ``title`` names BOTH teams ("Jordan vs Argentina Winner?"),
    # so either would match the home team for every leg. ``yes_sub_title``/``subtitle``
    # carry just this leg's side ("Jordan", "Tie"), which is what we classify on.
    label = _norm(
        " ".join(
            str(market.get(k, ""))
            for k in ("yes_sub_title", "yes_subtitle", "subtitle")
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
    """Find the event whose title mentions both teams and map its markets.

    Candidates are ranked by match specificity (summed longest-matched-token length),
    not payload order: with both "South Korea vs X" and "North Korea vs X" events open,
    the bare "korea" alias matches both titles, and only the score picks the right one.
    """
    candidates: list[tuple[int, int, dict[str, Any]]] = []
    for i, event in enumerate(events_payload.get("events", [])):
        text = _norm(
            " ".join(str(event.get(k, "")) for k in ("title", "sub_title", "subtitle"))
        )
        home_score = _team_match_score(text, home_team)
        away_score = _team_match_score(text, away_team)
        if home_score and away_score:
            # payload index breaks score ties, preserving first-match behaviour
            candidates.append((-(home_score + away_score), i, event))
    for _, _, event in sorted(candidates, key=lambda c: (c[0], c[1])):
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
