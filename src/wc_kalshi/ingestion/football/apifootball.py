"""API-Football (api-sports.io) live provider — PRIMARY real feed.

The pure mapping functions (``snapshot_from_payload`` and friends) are deliberately
separated from the network code so they are unit-tested offline against a captured
sample payload — we never need a live key or network access to test the mapping.

Cost note: API-Football's free tier is 100 req/day. Fetching per-fixture statistics
multiplies requests, so live polling needs a paid plan (see research.md §2). The
default runtime provider is the simulator precisely so we never silently burn quota.
"""

from __future__ import annotations

from typing import Any

import httpx

from ...logging_setup import get_logger
from ...models.schemas import MatchContext, MatchPeriod, MatchSnapshot, TeamStats
from ..http import request_with_retry
from .base import FootballDataProvider

log = get_logger("football.apifootball")

_PERIOD_MAP = {
    "NS": MatchPeriod.PRE,
    "1H": MatchPeriod.FIRST_HALF,
    "HT": MatchPeriod.HALF_TIME,
    "2H": MatchPeriod.SECOND_HALF,
    "ET": MatchPeriod.ET_FIRST,
    "BT": MatchPeriod.ET_BREAK,
    "P": MatchPeriod.PENALTIES,
    "FT": MatchPeriod.FULL_TIME,
    "AET": MatchPeriod.FULL_TIME,
    "PEN": MatchPeriod.FULL_TIME,
}


def parse_period(short: str | None) -> MatchPeriod:
    return _PERIOD_MAP.get((short or "").upper(), MatchPeriod.FIRST_HALF)


def _to_float(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, str):
        value = value.strip().rstrip("%")
        if value in {"", "-"}:
            return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _to_int(value: Any) -> int:
    return int(round(_to_float(value)))


def team_stats_from_statistics(stat_block: dict[str, Any] | None) -> TeamStats:
    """Map one team's API-Football ``statistics`` array into ``TeamStats``."""
    stats = TeamStats()
    if not stat_block:
        return stats
    items = {
        str(item.get("type", "")).strip().lower(): item.get("value")
        for item in stat_block.get("statistics", [])
    }
    stats.shots = _to_int(items.get("total shots"))
    stats.shots_on_target = _to_int(items.get("shots on goal"))
    stats.corners = _to_int(items.get("corner kicks"))
    stats.offsides = _to_int(items.get("offsides"))
    stats.fouls = _to_int(items.get("fouls"))
    stats.yellow_cards = _to_int(items.get("yellow cards"))
    stats.red_cards = _to_int(items.get("red cards"))
    stats.gk_saves = _to_int(items.get("goalkeeper saves"))
    stats.xg = _to_float(items.get("expected_goals"))
    poss = _to_float(items.get("ball possession"))
    stats.possession = poss / 100.0 if poss > 1.0 else (poss or 0.5)
    pa = _to_float(items.get("passes %"))
    stats.pass_accuracy = pa / 100.0 if pa > 1.0 else pa
    stats.big_chances = _to_int(items.get("big chances"))
    return stats


def _index_statistics(stats_response: list[dict[str, Any]] | None) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for block in stats_response or []:
        name = (block.get("team") or {}).get("name")
        if name:
            out[name] = block
    return out


def snapshot_from_payload(
    fixture: dict[str, Any],
    stats_response: list[dict[str, Any]] | None = None,
    events_response: list[dict[str, Any]] | None = None,
) -> MatchSnapshot:
    """Build a normalized ``MatchSnapshot`` from API-Football JSON blocks."""
    fx = fixture.get("fixture", {})
    teams = fixture.get("teams", {})
    goals = fixture.get("goals", {})
    home_name = (teams.get("home") or {}).get("name", "Home")
    away_name = (teams.get("away") or {}).get("name", "Away")
    status = fx.get("status", {})
    short = status.get("short")
    period = parse_period(short)

    by_team = _index_statistics(stats_response)
    home_stats = team_stats_from_statistics(by_team.get(home_name))
    away_stats = team_stats_from_statistics(by_team.get(away_name))

    # Fall back to counting red cards from events if statistics omit them.
    if events_response:
        for ev in events_response:
            if str(ev.get("type", "")).lower() == "card" and "red" in str(
                ev.get("detail", "")
            ).lower():
                ev_team = (ev.get("team") or {}).get("name")
                if ev_team == home_name and home_stats.red_cards == 0:
                    home_stats.red_cards += 1
                elif ev_team == away_name and away_stats.red_cards == 0:
                    away_stats.red_cards += 1

    venue = (fx.get("venue") or {}).get("name")
    context = MatchContext(neutral_venue=True, venue=venue)

    status_str = (
        "finished" if period.is_finished else ("live" if period.is_live else "scheduled")
    )
    return MatchSnapshot(
        match_id=str(fx.get("id", f"{home_name}-{away_name}")),
        provider="apifootball",
        home_team=home_name,
        away_team=away_name,
        minute=_to_int(status.get("elapsed")),
        period=period,
        home_score=_to_int(goals.get("home")),
        away_score=_to_int(goals.get("away")),
        home=home_stats,
        away=away_stats,
        status=status_str,
        context=context,
        raw=fixture,
    )


class APIFootballProvider(FootballDataProvider):
    name = "apifootball"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://v3.football.api-sports.io",
        timeout: float = 10.0,
        max_retries: int = 3,
        fetch_statistics: bool = True,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.max_retries = max_retries
        self.fetch_statistics = fetch_statistics
        self._client = httpx.AsyncClient(
            timeout=timeout, headers={"x-apisports-key": api_key}
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get(self, endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
        resp = await request_with_retry(
            self._client,
            "GET",
            f"{self.base_url}{endpoint}",
            params=params,
            max_retries=self.max_retries,
        )
        resp.raise_for_status()
        return resp.json()

    async def fetch_live(self) -> list[MatchSnapshot]:
        data = await self._get("/fixtures", {"live": "all"})
        fixtures = data.get("response", [])
        snapshots: list[MatchSnapshot] = []
        for fixture in fixtures:
            stats = events = None
            if self.fetch_statistics:
                fixture_id = (fixture.get("fixture") or {}).get("id")
                try:
                    stats = (
                        await self._get("/fixtures/statistics", {"fixture": fixture_id})
                    ).get("response")
                except Exception as exc:  # degrade gracefully
                    log.warning("statistics fetch failed", extra={"err": str(exc)})
            snapshots.append(snapshot_from_payload(fixture, stats, events))
        return snapshots
