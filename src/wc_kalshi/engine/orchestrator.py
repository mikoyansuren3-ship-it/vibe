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
        self.processor = TickProcessor(
            rt, trade=trade, decision_mode=rt.cfg.execution.decision_mode
        )
        self.states: dict[str, MatchState] = {}
        self._stop = asyncio.Event()
        self._flatten_requested = False
        self._last_live: list[MatchSnapshot] = []
        # Settlement tracking: matches seen live last poll, those awaiting a settled tick
        # (retried each poll until finished — the API lags between dropping a match from the
        # live feed and marking it FT), and those already settled.
        self._live_ids: set[str] = set()
        self._pending_settle: dict[str, int] = {}  # match_id -> settle attempts
        self._settled_ids: set[str] = set()
        self._settle_max_attempts = 40  # ~ minutes of retrying before giving up

    def stop(self) -> None:
        self._stop.set()

    def kill(self, reason: str = "manual kill switch") -> None:
        self.rt.risk.engage_kill_switch(reason)
        # Flatten open inventory on shutdown if configured (place closing orders).
        if self.rt.cfg.execution.flatten_on_kill:
            self._flatten_requested = True
        self._stop.set()

    def _interval(self) -> float:
        cfg = self.rt.cfg.football
        if self.provider.name == "simulated":
            return max(0.0, cfg.sim_tick_seconds)
        if not getattr(cfg, "adaptive_polling", True):
            return max(0.5, cfg.poll_interval_seconds)
        return self._adaptive_interval()

    def _adaptive_interval(self) -> float:
        """Poll fast when a live match is close & late, slow when idle, normal otherwise.

        Finished matches naturally drop out of ``fetch_live`` (so they pause). This
        keeps us well inside the request budget during blowouts/quiet periods while
        reacting quickly in the minutes that actually move prices.
        """
        cfg = self.rt.cfg.football
        live = [m for m in self._last_live if m.period.is_live]
        if not live:
            return max(1.0, cfg.poll_interval_idle_seconds)
        urgent = any(
            m.minute >= 70 and abs(m.score_diff) <= 1 for m in live
        )
        if urgent:
            return max(0.5, cfg.poll_interval_fast_seconds)
        return max(0.5, cfg.poll_interval_seconds)

    async def _handle(self, match: MatchSnapshot) -> None:
        rt = self.rt
        if match.status == "abandoned":  # interrupted/suspended/etc — no valid result
            return
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

    async def _settle_dropped(self, current_ids: set[str]) -> None:
        """Capture the settled state of matches that have left the live feed.

        A match dropping from ``fetch_live`` doesn't mean the API reports it FT yet, so we
        keep RETRYING each poll until it's finished (or we give up), rather than checking
        only the single poll it disappeared — otherwise a match that isn't FT on that exact
        poll is forgotten and never settles.
        """
        for mid in self._live_ids - current_ids - self._settled_ids:
            self._pending_settle.setdefault(mid, 0)
        self._live_ids = current_ids
        for mid in list(self._pending_settle):
            if mid in current_ids:  # came back live (transient flap) — stop trying
                del self._pending_settle[mid]
                continue
            self._pending_settle[mid] += 1
            try:
                snap = await self.provider.fetch_fixture(mid)
            except Exception as exc:  # one bad settle must not stall the loop
                log.warning("settlement fetch failed", extra={"match_id": mid, "err": str(exc)})
                snap = None
            if snap is not None and snap.status == "abandoned":
                # interrupted/suspended/postponed — no valid 90' result; stop retrying.
                self._settled_ids.add(mid)
                del self._pending_settle[mid]
                log.info("match abandoned/void — not settling", extra={"match_id": mid})
            elif snap is not None and snap.period.is_finished:
                self._settled_ids.add(mid)
                del self._pending_settle[mid]
                log.info(
                    "captured final state",
                    extra={"match_id": mid, "score": f"{snap.home_score}-{snap.away_score}"},
                )
                await self._handle(snap)
            elif self._pending_settle[mid] >= self._settle_max_attempts:
                log.warning("settlement gave up (never reported FT)", extra={"match_id": mid})
                del self._pending_settle[mid]

    async def run(self, *, max_ticks: int | None = None) -> None:
        rt = self.rt
        log.info("orchestrator starting", extra={"mode": rt.cfg.mode.value, "provider": self.provider.name})
        rt.audit.log("boot", f"orchestrator start mode={rt.cfg.mode.value}", provider=self.provider.name)
        tick = 0
        try:
            while not self._stop.is_set():
                # A transient network/API error (DNS blip, 5xx, machine waking from sleep)
                # must NOT kill a long-running recorder — log it and retry next interval.
                try:
                    matches = await self.provider.fetch_live()
                    self._last_live = matches or []
                    if matches:
                        await asyncio.gather(*(self._handle(m) for m in matches))
                    # Capture the final/settled state of any match that just dropped out of
                    # the live feed (it finished), so replay can settle + score it.
                    await self._settle_dropped({m.match_id for m in self._last_live})
                    await self._maybe_sweep_resting()
                except Exception as exc:  # noqa: BLE001 - keep the loop alive across blips
                    log.warning("poll failed; retrying next interval", extra={"err": str(exc)})
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
            if self._flatten_requested:
                try:
                    from .trading import flatten_all

                    await flatten_all(rt, reason="kill switch flatten")
                except Exception as exc:  # never let flatten mask the shutdown
                    log.exception("flatten on kill failed", extra={"err": str(exc)})
            rt.audit.log(
                "shutdown",
                "orchestrator stopped",
                ticks=tick,
                **{k: round(v, 2) if isinstance(v, float) else v for k, v in rt.portfolio.snapshot().items() if not isinstance(v, dict)},
            )
            log.info("orchestrator stopped", extra={"ticks": tick})

    async def _maybe_sweep_resting(self) -> None:
        timeout = self.rt.cfg.execution.resting_timeout_seconds
        if timeout and timeout > 0 and self.rt.resting_orders:
            from .trading import sweep_resting_orders

            await sweep_resting_orders(self.rt, timeout_seconds=timeout)

    @property
    def all_settled(self) -> bool:
        return all(s.settled for s in self.states.values()) and bool(self.states)
