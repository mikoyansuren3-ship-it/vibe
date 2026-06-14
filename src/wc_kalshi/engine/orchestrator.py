"""Async orchestrator: poll the football feed, fan out per match, run the pipeline.

Each poll fetches live matches, then processes them concurrently (per-match state +
async market fetch). Persists raw match/market snapshots (append-only) before the
pipeline runs, so the whole session is replayable. Honours a cooperative stop and
the global kill switch.
"""

from __future__ import annotations

import asyncio

from ..eventbus import Event, EventType
from ..ingestion.football.base import FootballDataProvider
from ..logging_setup import get_logger
from ..models.schemas import MatchSnapshot
from .builders import Runtime
from .match_loop import MatchState, TickProcessor

log = get_logger("engine.orchestrator")


class Orchestrator:
    def __init__(self, rt: Runtime, provider: FootballDataProvider, *, trade: bool = True) -> None:
        self.rt = rt
        self.provider = provider
        self.processor = TickProcessor(rt, trade=trade)
        self.states: dict[str, MatchState] = {}
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    def kill(self, reason: str = "manual kill switch") -> None:
        self.rt.risk.engage_kill_switch(reason)
        self._stop.set()

    def _interval(self) -> float:
        if self.provider.name == "simulated":
            return max(0.0, self.rt.cfg.football.sim_tick_seconds)
        return max(0.5, self.rt.cfg.football.poll_interval_seconds)

    async def _handle(self, match: MatchSnapshot) -> None:
        rt = self.rt
        try:
            rt.db.add_match_snapshot(match)
            rt.bus.publish(Event(EventType.MATCH_SNAPSHOT, {"match_id": match.match_id, "minute": match.minute}, match.match_id))
            snaps = await rt.market_feed.snapshots_for_match(match)
            for s in snaps:
                rt.db.add_market_snapshot(s)
            st = self.states.setdefault(match.match_id, MatchState(match.match_id))
            await self.processor.process(match, snaps, st)
        except Exception as exc:  # one bad match must not kill the whole run
            log.exception("match handling failed", extra={"match_id": match.match_id, "err": str(exc)})

    async def run(self, *, max_ticks: int | None = None) -> None:
        rt = self.rt
        log.info("orchestrator starting", extra={"mode": rt.cfg.mode.value, "provider": self.provider.name})
        rt.audit.log("boot", f"orchestrator start mode={rt.cfg.mode.value}", provider=self.provider.name)
        tick = 0
        try:
            while not self._stop.is_set():
                matches = await self.provider.fetch_live()
                if matches:
                    await asyncio.gather(*(self._handle(m) for m in matches))
                tick += 1

                if getattr(self.provider, "all_finished", False):
                    log.info("all matches finished")
                    break
                if max_ticks is not None and tick >= max_ticks:
                    break
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self._interval())
                except asyncio.TimeoutError:
                    pass
        finally:
            rt.audit.log(
                "shutdown",
                "orchestrator stopped",
                ticks=tick,
                **{k: round(v, 2) if isinstance(v, float) else v for k, v in rt.portfolio.snapshot().items() if not isinstance(v, dict)},
            )
            log.info("orchestrator stopped", extra={"ticks": tick})

    @property
    def all_settled(self) -> bool:
        return all(s.settled for s in self.states.values()) and bool(self.states)
