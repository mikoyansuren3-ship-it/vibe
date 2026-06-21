"""Historical real-data loader for the backtester.

The synthetic backtest can only ever tell you the strategy is self-consistent; it
cannot tell you the edge is *real*, because the synthetic market is something we
invented. To measure real edge you need real inputs:

  * a real xG (and score/red-card) timeline per match, and
  * the market prices that were actually quoted at those moments.

This module maps that into the same ``(MatchSnapshot, [MarketSnapshot])`` ticks the
replay engine already consumes, so ``Backtester.run_historical`` can score the model
and detect edge against **real closing lines**.

Graceful degradation (per the plan):
  * If a tick has ``markets`` -> we trade and can measure edge / CLV vs real prices.
  * If a match has NO market prices anywhere -> we still score model calibration on
    the real outcome (xG-only mode); market edge stays an explicitly open question.

File format (JSON): a single match object, a list of match objects, or JSON Lines
(one match object per line). One match object::

    {
      "match_id": "ARG-FRA-2022-final",
      "home_team": "Argentina", "away_team": "France",
      "home_elo": 2105, "away_elo": 2078, "neutral_venue": true,
      "ticks": [
        {"minute": 0,  "period": "1H", "home_score": 0, "away_score": 0,
         "home_xg": 0.0, "away_xg": 0.0},
        {"minute": 23, "period": "1H", "home_score": 1, "away_score": 0,
         "home_xg": 0.55, "away_xg": 0.20,
         "markets": {"home": [54, 56], "draw": [26, 28], "away": [16, 18]}},
        {"minute": 90, "period": "FT", "home_score": 3, "away_score": 3,
         "home_xg": 2.3, "away_xg": 2.7}
      ]
    }

``markets`` maps an outcome ("home"/"draw"/"away") to ``[yes_bid_cents, yes_ask_cents]``.
The final tick SHOULD have ``period: "FT"`` so the match settles on its real result.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..models.schemas import (
    BookLevel,
    MarketSnapshot,
    MatchContext,
    MatchPeriod,
    MatchSnapshot,
    Outcome,
    TeamStats,
)

HistoricalTick = tuple[MatchSnapshot, list[MarketSnapshot]]


def _period(value: Any) -> MatchPeriod:
    if isinstance(value, MatchPeriod):
        return value
    try:
        return MatchPeriod(str(value))
    except ValueError:
        return MatchPeriod.FIRST_HALF


def _market_snaps(match_id: str, minute: int, markets: dict[str, Any] | None) -> list[MarketSnapshot]:
    if not markets:
        return []
    out: list[MarketSnapshot] = []
    for outcome in (Outcome.HOME, Outcome.DRAW, Outcome.AWAY):
        quote = markets.get(outcome.value)
        if quote is None:
            continue
        if isinstance(quote, dict):
            yes_bid = quote.get("yes_bid")
            yes_ask = quote.get("yes_ask")
            last = quote.get("last_price")
        else:  # [bid, ask] pair
            yes_bid, yes_ask = int(quote[0]), int(quote[1])
            last = None
        depth_bid = int(yes_bid) if yes_bid is not None else None
        out.append(
            MarketSnapshot(
                market_ticker=f"HIST-{match_id}-{outcome.value}",
                event_ticker=f"HIST-{match_id}",
                match_id=match_id,
                outcome=outcome,
                yes_bid=yes_bid,
                yes_ask=yes_ask,
                last_price=last,
                # A single book level at the quote so the book-walk fill model has depth.
                yes_depth=[BookLevel(price_cents=depth_bid, size=500)] if depth_bid else [],
                no_depth=(
                    [BookLevel(price_cents=int(100 - yes_ask), size=500)]
                    if yes_ask is not None
                    else []
                ),
                status="active",
            )
        )
    return out


def load_historical_match(data: dict[str, Any]) -> list[HistoricalTick]:
    """Map one historical match dict into replayable ticks."""
    match_id = str(data["match_id"])
    home_team = str(data.get("home_team", "Home"))
    away_team = str(data.get("away_team", "Away"))
    context = MatchContext(
        neutral_venue=bool(data.get("neutral_venue", True)),
        home_elo=data.get("home_elo"),
        away_elo=data.get("away_elo"),
    )
    ticks: list[HistoricalTick] = []
    for t in data.get("ticks", []):
        snap = MatchSnapshot(
            match_id=match_id,
            provider="historical",
            home_team=home_team,
            away_team=away_team,
            minute=int(t.get("minute", 0)),
            period=_period(t.get("period", "1H")),
            home_score=int(t.get("home_score", 0)),
            away_score=int(t.get("away_score", 0)),
            home=TeamStats(
                xg=float(t.get("home_xg", 0.0)), red_cards=int(t.get("home_red", 0))
            ),
            away=TeamStats(
                xg=float(t.get("away_xg", 0.0)), red_cards=int(t.get("away_red", 0))
            ),
            status="finished" if _period(t.get("period", "1H")).is_finished else "live",
            context=context,
        )
        ticks.append((snap, _market_snaps(match_id, snap.minute, t.get("markets"))))
    return ticks


def load_historical_file(path: str | Path) -> list[list[HistoricalTick]]:
    """Load one or many historical matches from JSON or JSON Lines."""
    text = Path(path).read_text().strip()
    if not text:
        return []
    matches: list[dict[str, Any]] = []
    try:
        parsed = json.loads(text)
        matches = parsed if isinstance(parsed, list) else [parsed]
    except json.JSONDecodeError:
        # JSON Lines: one match object per line.
        for line in text.splitlines():
            line = line.strip()
            if line:
                matches.append(json.loads(line))
    return [load_historical_match(m) for m in matches]


def has_market_data(matches: list[list[HistoricalTick]]) -> bool:
    """True if any tick carries real market prices (else we're in xG-only mode)."""
    return any(mk for match in matches for _snap, mk in match)
