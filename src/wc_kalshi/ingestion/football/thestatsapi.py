"""TheStatsAPI live provider — FALLBACK, chosen for live xG coverage.

The exact response shapes are documented per-plan; the mapping below encodes our
best understanding (research.md §2) and is marked [ASSUMPTION] where the field
path isn't authoritatively confirmed. Like the API-Football provider, the pure
mapping (``snapshot_from_match``) is isolated for offline unit-testing, and the
network layer degrades gracefully when a field is missing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx

from ...logging_setup import get_logger
from ...models.schemas import MatchContext, MatchPeriod, MatchSnapshot, TeamStats
from ...modeling.ratings import apply_ratings
from ..http import request_with_retry
from .base import FootballDataProvider

if TYPE_CHECKING:
    from ..budget import RequestBudget

log = get_logger("football.thestatsapi")


def _num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _period(status: str | None, minute: int) -> MatchPeriod:
    s = (status or "").lower()
    if s in {"ft", "finished", "ended"}:
        return MatchPeriod.FULL_TIME
    if s in {"ht", "halftime"}:
        return MatchPeriod.HALF_TIME
    if "extra" in s:
        return MatchPeriod.ET_FIRST
    if s in {"ns", "scheduled", "not started"}:
        return MatchPeriod.PRE
    return MatchPeriod.SECOND_HALF if minute > 45 else MatchPeriod.FIRST_HALF


def snapshot_from_match(match: dict[str, Any], stats: dict[str, Any] | None = None) -> MatchSnapshot:
    """Map a TheStatsAPI match (+ optional stats) into a ``MatchSnapshot``. [ASSUMPTION]"""
    home_name = match.get("home_team") or (match.get("home") or {}).get("name", "Home")
    away_name = match.get("away_team") or (match.get("away") or {}).get("name", "Away")
    minute = int(_num(match.get("minute") or match.get("elapsed")))
    period = _period(match.get("status"), minute)

    home = TeamStats()
    away = TeamStats()
    s = stats or match.get("stats") or {}
    # xg has None semantics (schemas.TeamStats): "provider did not supply it" must stay
    # None so the shot proxy / prior can take over. Coercing a missing value to 0.0
    # would read as "zero chances created" — from the provider chosen FOR its xG, and
    # precisely when its /stats call failed (fetch_live swallows that and passes None).
    hx = (s.get("home") or {}).get("xg")
    ax = (s.get("away") or {}).get("xg")
    home.xg = _num(hx) if hx is not None else None
    away.xg = _num(ax) if ax is not None else None
    home.shots = int(_num((s.get("home") or {}).get("shots")))
    away.shots = int(_num((s.get("away") or {}).get("shots")))
    home.shots_on_target = int(_num((s.get("home") or {}).get("shots_on_target")))
    away.shots_on_target = int(_num((s.get("away") or {}).get("shots_on_target")))
    home.red_cards = int(_num((s.get("home") or {}).get("red_cards")))
    away.red_cards = int(_num((s.get("away") or {}).get("red_cards")))
    poss = _num((s.get("home") or {}).get("possession"))
    if poss:
        home.possession = poss / 100.0 if poss > 1 else poss
        away.possession = 1 - home.possession

    status_str = (
        "finished" if period.is_finished else ("live" if period.is_live else "scheduled")
    )
    return MatchSnapshot(
        match_id=str(match.get("id") or match.get("match_id") or f"{home_name}-{away_name}"),
        provider="thestatsapi",
        home_team=home_name,
        away_team=away_name,
        minute=minute,
        period=period,
        home_score=int(_num(match.get("home_score") or (match.get("score") or {}).get("home"))),
        away_score=int(_num(match.get("away_score") or (match.get("score") or {}).get("away"))),
        home=home,
        away=away,
        status=status_str,
        context=apply_ratings(MatchContext(), home_name, away_name),
        raw=match,
    )


class TheStatsAPIProvider(FootballDataProvider):
    name = "thestatsapi"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://api.thestatsapi.com",
        timeout: float = 10.0,
        max_retries: int = 3,
        budget: "RequestBudget | None" = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.max_retries = max_retries
        self._budget = budget
        self._client = httpx.AsyncClient(
            timeout=timeout, headers={"Authorization": f"Bearer {api_key}"}
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get(self, endpoint: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        resp = await request_with_retry(
            self._client,
            "GET",
            f"{self.base_url}{endpoint}",
            params=params,
            max_retries=self.max_retries,
            # Spend a budget token per wire attempt (retries included), not once per call.
            on_attempt=self._budget.acquire if self._budget is not None else None,
        )
        resp.raise_for_status()
        return resp.json()

    async def fetch_live(self) -> list[MatchSnapshot]:
        # [ASSUMPTION] live matches listed at /football/matches?status=live
        data = await self._get("/football/matches", {"status": "live"})
        matches = data.get("data") or data.get("matches") or []
        out: list[MatchSnapshot] = []
        for match in matches:
            stats = None
            mid = match.get("id") or match.get("match_id")
            try:
                stats = (await self._get(f"/football/matches/{mid}/stats")).get("data")
            except Exception as exc:
                log.warning("xg stats fetch failed", extra={"err": str(exc)})
            out.append(snapshot_from_match(match, stats))
        return out
