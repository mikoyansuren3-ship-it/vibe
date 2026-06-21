"""Football data provider interface + factory.

A provider's single job: return the current normalized ``MatchSnapshot`` for every
match it considers live, each time ``fetch_live`` is called. The orchestrator polls
on an interval. Stateful providers (the simulator) advance their internal clock on
each call; stateless ones (API-Football) just map the latest API response.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from ...models.schemas import MatchSnapshot

if TYPE_CHECKING:
    from ...config import AppConfig


class FootballDataProvider(ABC):
    name: str = "base"

    @abstractmethod
    async def fetch_live(self) -> list[MatchSnapshot]:
        """Return current snapshots for all live matches (possibly empty)."""

    async def fetch_fixture(self, match_id: str) -> "MatchSnapshot | None":
        """Return the current snapshot for one fixture by id, in ANY state.

        Used to capture a match's FINAL/settled state after it drops out of
        ``fetch_live`` (which only returns in-play matches). Default: unsupported.
        """
        return None

    async def aclose(self) -> None:  # pragma: no cover - default no-op
        return None


def build_football_provider(cfg: "AppConfig") -> FootballDataProvider:
    """Construct the configured provider. Defaults to the offline simulator.

    Live providers share a single ``RequestBudget`` (token bucket) sized to the daily
    quota so the aggregate request rate across all concurrent matches stays bounded.
    """
    provider = cfg.football.provider.lower()
    if provider == "simulated":
        from .simulated import SimulatedFootballProvider

        return SimulatedFootballProvider(
            seed=cfg.football.sim_seed,
            minutes_per_tick=cfg.football.sim_minutes_per_tick,
        )

    from ..budget import RequestBudget

    budget = RequestBudget(cfg.football.daily_request_budget)
    if provider == "apifootball":
        from .apifootball import APIFootballProvider

        if not cfg.secrets.apifootball_key:
            raise ValueError("football.provider=apifootball but APIFOOTBALL_KEY is not set")
        return APIFootballProvider(
            api_key=cfg.secrets.apifootball_key,
            base_url=cfg.football.apifootball_base,
            timeout=cfg.football.request_timeout_seconds,
            max_retries=cfg.football.max_retries,
            fetch_statistics=cfg.football.apifootball_fetch_statistics,
            fetch_context=cfg.football.apifootball_fetch_context,
            league_id=cfg.football.apifootball_league_id,
            budget=budget,
        )
    if provider == "thestatsapi":
        from .thestatsapi import TheStatsAPIProvider

        if not cfg.secrets.thestatsapi_key:
            raise ValueError("football.provider=thestatsapi but THESTATSAPI_KEY is not set")
        return TheStatsAPIProvider(
            api_key=cfg.secrets.thestatsapi_key,
            base_url=cfg.football.thestatsapi_base,
            timeout=cfg.football.request_timeout_seconds,
            max_retries=cfg.football.max_retries,
            budget=budget,
        )
    raise ValueError(f"Unknown football provider: {cfg.football.provider!r}")
