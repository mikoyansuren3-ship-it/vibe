"""Async orchestrator: poll the football feed, fan out per match, run the pipeline.

Each poll fetches live matches, then processes them concurrently (per-match state +
async market fetch). Persists raw match/market snapshots (append-only) before the
pipeline runs, so the whole session is replayable. Honours a cooperative stop and
the global kill switch.
"""

from __future__ import annotations

import asyncio
import time

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
            rt,
            trade=trade,
            decision_mode=rt.cfg.execution.decision_mode,
            # Live loop marks the whole book + snapshots ONCE per poll (book_epilogue below),
            # not once per match — see TickProcessor.book_epilogue.
            defer_book_epilogue=True,
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
        self._extra_last: dict[str, float] = {}  # match_id -> last extra-market capture (mono)
        self._extra_tasks: set[asyncio.Task] = set()  # in-flight background captures

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
        snaps: list = []
        try:
            rt.db.add_match_snapshot(match)
            rt.bus.publish(Event(EventType.MATCH_SNAPSHOT, {"match_id": match.match_id, "minute": match.minute}, match.match_id))
            snaps = await rt.market_feed.snapshots_for_match(match)
            rt.db.add_market_snapshots(snaps)
            st = self.states.setdefault(match.match_id, MatchState(match.match_id))
            await self.processor.process(match, snaps, st)
        except Exception as exc:  # one bad match must not kill the whole run
            log.exception("match handling failed", extra={"match_id": match.match_id, "err": str(exc)})
        # The broader-market capture is research-only (feeds raw_market_quotes, never the
        # decision) yet costs many serial Kalshi RTTs. Spawn it in the BACKGROUND after the
        # trade path so it can't inject ~6 s of latency ahead of an order.
        self._spawn_extra_capture(match, snaps)

    def _spawn_extra_capture(self, match: MatchSnapshot, snaps: list) -> None:
        """Fire-and-forget the extra-market capture (Total/Spread/BTTS/1H/corners/…) into
        raw_market_quotes. Throttled + gated here synchronously so we don't even create a
        task when it isn't due; the task is tracked so it isn't GC'd and is drained on stop.
        """
        rt = self.rt
        if not getattr(rt.cfg.kalshi, "capture_extra_markets", False):
            return
        client = getattr(rt.market_feed, "client", None)
        if client is None or not snaps:  # paper/sim feed has no client
            return
        # The KXWCGAME event ticker shares its suffix with every other per-match series.
        ev = snaps[0].event_ticker or ""
        if "-" not in ev:
            return
        now = time.monotonic()
        if now - self._extra_last.get(match.match_id, 0.0) < rt.cfg.kalshi.extra_markets_interval_seconds:
            return
        self._extra_last[match.match_id] = now
        task = asyncio.create_task(
            self._capture_extra_markets(client, match.match_id, ev.split("-", 1)[1])
        )
        self._extra_tasks.add(task)
        task.add_done_callback(self._extra_tasks.discard)

    async def _capture_extra_markets(self, client, match_id: str, event_suffix: str) -> None:
        """Background body: pull the roadmap series for one fixture and persist the quotes.
        Kept serial (not gathered) so a burst doesn't drain the shared Kalshi read limiter
        and add jitter to the decision path — nothing waits on this, so 6 s in the
        background is fine."""
        from ..ingestion.kalshi.extra_markets import capture_extra_markets

        try:
            rows = await capture_extra_markets(client, match_id, event_suffix)
            self.rt.db.add_raw_market_quotes(rows)
        except Exception as exc:
            log.warning("extra-market capture failed", extra={"match_id": match_id, "err": str(exc)})

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
                # Book any late fills on this match's resting orders before it leaves the
                # watch set, so the settled position reflects the true exchange fills.
                from .trading import reconcile_resting_orders

                await reconcile_resting_orders(self.rt, match_id=mid)
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
                    # Bound the poll: a wedged endpoint (or a stacked-up Retry-After) must
                    # not freeze the loop with open positions we can't react to. On timeout
                    # we skip the tick entirely (no settle) and retry next interval — a
                    # dropped-match false-settle from an empty live set would be worse.
                    matches = await asyncio.wait_for(
                        self.provider.fetch_live(),
                        timeout=rt.cfg.football.poll_timeout_seconds,
                    )
                    self._last_live = matches or []
                    if matches:
                        await asyncio.gather(*(self._handle(m) for m in matches))
                    # Capture the final/settled state of any match that just dropped out of
                    # the live feed (it finished), so replay can settle + score it.
                    await self._settle_dropped({m.match_id for m in self._last_live})
                    await self._maybe_sweep_resting()
                    # One whole-book mark-to-market + risk/portfolio snapshot for the WHOLE
                    # poll (positions marked, settlements booked, resting sweeps done) —
                    # instead of the same O(positions) work repeated inside every match.
                    self.processor.book_epilogue()
                except asyncio.TimeoutError:
                    log.warning(
                        "live poll timed out; skipping tick",
                        extra={"timeout_s": rt.cfg.football.poll_timeout_seconds},
                    )
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
            # Cancel any in-flight background captures so they don't outlive the loop or
            # write to a closing DB (research-only; a dropped snapshot on stop is harmless).
            for t in self._extra_tasks:
                t.cancel()
            if self._extra_tasks:
                await asyncio.gather(*self._extra_tasks, return_exceptions=True)
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
