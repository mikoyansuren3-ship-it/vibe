"""Execution lifecycle: kill-switch flatten, resting-order timeout, adaptive polling."""

import asyncio
from datetime import timedelta

import pytest

from wc_kalshi.engine import trading
from wc_kalshi.engine.builders import build_runtime
from wc_kalshi.engine.orchestrator import Orchestrator
from wc_kalshi.engine.trading import flatten_all, sweep_resting_orders
from wc_kalshi.execution.base import Executor, Fill
from wc_kalshi.ingestion.football.base import FootballDataProvider
from wc_kalshi.models.schemas import MatchPeriod, OrderAction, Outcome
from wc_kalshi.util import utcnow


class _LateFillExecutor(Executor):
    """Stub live-style executor: ``fills_for`` returns a controllable, growing fill set so we
    can drive late-fill reconciliation deterministically."""

    def __init__(self):
        super().__init__()
        self.fills: list[Fill] = []
        self.canceled: list[str] = []

    async def _place(self, order, market):  # pragma: no cover - placement not exercised here
        raise AssertionError("placement not used in reconcile tests")

    async def fills_for(self, oid, **kw):
        return list(self.fills)

    async def cancel(self, oid):
        self.canceled.append(oid)
        return True


def _rest(rt, oid="EX-1", *, booked=0, stale=False):
    rt.resting_orders[oid] = {
        "coid": "c", "match_id": "m1", "market_ticker": "T1", "outcome": Outcome.HOME,
        "action": OrderAction.BUY, "cost_per_contract": 0.5, "limit_price_cents": 50,
        "booked": booked, "minute": 30,
        "placed_ts": utcnow() - timedelta(seconds=120 if stale else 0),
    }


@pytest.fixture
def rt(cfg):
    return build_runtime(cfg)


async def test_flatten_all_closes_open_positions(rt):
    # Open a long-Yes position directly in the portfolio.
    rt.portfolio.apply_fill(
        match_id="m1", market_ticker="KX-1", outcome=Outcome.HOME,
        action=OrderAction.BUY, contracts=50, price_cents=40, fee=0.0,
    )
    assert rt.portfolio.positions["KX-1"].net_yes == 50
    n = await flatten_all(rt, persist=False)
    assert n == 1
    # After flatten the market is net-flat (netting removes the offsetting lots).
    pos = rt.portfolio.positions.get("KX-1")
    assert pos is None or pos.net_yes == 0


async def test_sweep_cancels_stale_resting_orders(rt):
    rt.resting_orders["O1"] = {"coid": "c1", "match_id": "m1", "market_ticker": "t", "placed_ts": utcnow() - timedelta(seconds=120)}
    rt.resting_orders["O2"] = {"coid": "c2", "match_id": "m1", "market_ticker": "t", "placed_ts": utcnow()}
    canceled = await sweep_resting_orders(rt, timeout_seconds=60)
    assert canceled == 1
    assert "O1" not in rt.resting_orders  # stale one canceled
    assert "O2" in rt.resting_orders  # fresh one kept


class _StaticProvider(FootballDataProvider):
    name = "apifootball"

    def __init__(self, matches):
        self._matches = matches

    async def fetch_live(self):
        return self._matches

    async def aclose(self):
        pass


def test_adaptive_interval_fast_when_close_and_late(rt, match_factory):
    rt.cfg.football.adaptive_polling = True
    orch = Orchestrator(rt, _StaticProvider([]), trade=False)
    # close + late => fast
    orch._last_live = [match_factory(minute=85, home_score=1, away_score=1, period=MatchPeriod.SECOND_HALF)]
    assert orch._interval() == rt.cfg.football.poll_interval_fast_seconds
    # blowout / early => normal
    orch._last_live = [match_factory(minute=20, home_score=3, away_score=0, period=MatchPeriod.FIRST_HALF)]
    assert orch._interval() == rt.cfg.football.poll_interval_seconds
    # nothing live => idle
    orch._last_live = []
    assert orch._interval() == rt.cfg.football.poll_interval_idle_seconds


class _FinishingProvider(FootballDataProvider):
    """fetch_fixture returns ``not_finished`` until ``finish_after`` calls, then ``final``."""

    name = "apifootball"

    def __init__(self, final_snap, *, not_finished=None, finish_after=1):
        self._final = final_snap
        self._not_finished = not_finished
        self._finish_after = finish_after
        self.fixture_calls = 0

    async def fetch_live(self):
        return []

    async def fetch_fixture(self, match_id):
        self.fixture_calls += 1
        if self.fixture_calls < self._finish_after:
            return self._not_finished
        return self._final

    async def aclose(self):
        pass


async def test_settlement_captures_final_state(rt, match_factory):
    live = match_factory(match_id="m1", minute=80, period=MatchPeriod.SECOND_HALF,
                         home_score=1, away_score=1)
    final = match_factory(match_id="m1", minute=90, period=MatchPeriod.FULL_TIME,
                          home_score=2, away_score=1, status="finished")
    orch = Orchestrator(rt, _FinishingProvider(final), trade=False)
    # Poll 1: match is live (sets first_prob + registers the live id).
    await orch._handle(live)
    orch._live_ids = {"m1"}
    # Poll 2: match dropped out -> capture its settled state.
    await orch._settle_dropped(current_ids=set())
    snaps = rt.db.iter_match_snapshots("m1")
    assert any(s.period is MatchPeriod.FULL_TIME for s in snaps)  # finished tick persisted
    assert "m1" in orch._settled_ids
    assert rt.calibration.metrics()["n"] >= 1  # settled outcome scored
    # Idempotent: a later poll doesn't re-settle.
    before = orch._settled_ids.copy()
    await orch._settle_dropped(current_ids=set())
    assert orch._settled_ids == before


class _FlakyProvider(FootballDataProvider):
    """fetch_live raises on the first poll (network blip), succeeds after."""

    name = "apifootball"

    def __init__(self):
        self.calls = 0

    async def fetch_live(self):
        self.calls += 1
        if self.calls == 1:
            raise ConnectionError("nodename nor servname provided")
        return []

    async def aclose(self):
        pass


async def test_network_error_does_not_crash_recorder(rt):
    """A transient fetch_live failure must be swallowed so the long-running recorder
    survives blips (real bug: a DNS drop was crashing the launchd recorder)."""
    rt.cfg.football.poll_interval_idle_seconds = 0.01
    rt.cfg.football.poll_interval_seconds = 0.01
    prov = _FlakyProvider()
    orch = Orchestrator(rt, prov, trade=False)
    await orch.run(max_ticks=2)  # tick 1 raises, tick 2 succeeds — no exception escapes
    assert prov.calls == 2


async def test_transient_drop_is_not_settled(rt, match_factory):
    """A match that briefly vanishes but isn't actually finished must NOT be settled."""
    still_live = match_factory(match_id="m1", minute=81, period=MatchPeriod.SECOND_HALF)
    orch = Orchestrator(rt, _FinishingProvider(still_live), trade=False)
    orch._live_ids = {"m1"}
    await orch._settle_dropped(current_ids=set())
    assert "m1" not in orch._settled_ids  # not finished -> retry later, no false settle
    assert "m1" in orch._pending_settle  # stays queued for retry


async def test_settles_on_a_later_poll_not_just_the_drop_poll(rt, match_factory):
    """Regression: a match that isn't FT on the poll it drops out must still settle on a
    LATER poll (the API lags FT vs removing it from the live feed)."""
    not_ft = match_factory(match_id="m1", minute=90, period=MatchPeriod.SECOND_HALF)
    final = match_factory(match_id="m1", minute=90, period=MatchPeriod.FULL_TIME,
                          home_score=2, away_score=1, status="finished")
    prov = _FinishingProvider(final, not_finished=not_ft, finish_after=3)
    orch = Orchestrator(rt, prov, trade=False)
    await orch._handle(match_factory(match_id="m1", minute=80, period=MatchPeriod.SECOND_HALF))
    orch._live_ids = {"m1"}
    # Polls 1-2: API still not FT -> stays pending, not settled.
    await orch._settle_dropped(current_ids=set())
    await orch._settle_dropped(current_ids=set())
    assert "m1" not in orch._settled_ids and "m1" in orch._pending_settle
    # Poll 3: API now reports FT -> settles.
    await orch._settle_dropped(current_ids=set())
    assert "m1" in orch._settled_ids and "m1" not in orch._pending_settle
    assert any(s.period is MatchPeriod.FULL_TIME for s in rt.db.iter_match_snapshots("m1"))


async def test_abandoned_match_stops_retrying_without_settling(rt, match_factory):
    """An interrupted/abandoned match must stop retrying immediately and never get an FT tick."""
    abandoned = match_factory(match_id="ab1", minute=45, period=MatchPeriod.FIRST_HALF,
                              status="abandoned")
    prov = _FinishingProvider(abandoned, not_finished=abandoned, finish_after=99)
    orch = Orchestrator(rt, prov, trade=False)
    orch._live_ids = {"ab1"}
    await orch._settle_dropped(current_ids=set())
    assert "ab1" in orch._settled_ids and "ab1" not in orch._pending_settle  # done, not looping
    assert not any(s.period is MatchPeriod.FULL_TIME for s in rt.db.iter_match_snapshots("ab1"))


async def test_reappearing_match_clears_pending(rt, match_factory):
    """A match that flaps out then back to live must not be force-settled."""
    not_ft = match_factory(match_id="m1", minute=90, period=MatchPeriod.SECOND_HALF)
    final = match_factory(match_id="m1", minute=90, period=MatchPeriod.FULL_TIME, status="finished")
    prov = _FinishingProvider(final, not_finished=not_ft, finish_after=99)  # never FT
    orch = Orchestrator(rt, prov, trade=False)
    orch._live_ids = {"m1"}
    await orch._settle_dropped(current_ids=set())   # drops -> pending (not finished)
    assert "m1" in orch._pending_settle
    await orch._settle_dropped(current_ids={"m1"})  # back live -> cleared
    assert "m1" not in orch._pending_settle and "m1" not in orch._settled_ids


async def test_extra_capture_runs_after_process_in_background(rt, match_factory):
    """The research-only extra-market capture must be fire-and-forget AFTER the decision,
    never an inline block ahead of it — otherwise it injects ~6 s of Kalshi RTTs between
    the quote and the order."""
    from wc_kalshi.models.schemas import MarketSnapshot

    order: list[str] = []

    class _Client:
        async def get_markets(self, *, event_ticker=None, **kw):
            order.append("capture")
            return {"markets": []}

    class _Feed:
        client = _Client()

        async def snapshots_for_match(self, match):
            return [
                MarketSnapshot(
                    market_ticker="KXWCGAME-26JUN27USAWAL-USA",
                    event_ticker="KXWCGAME-26JUN27USAWAL",
                    match_id=match.match_id, outcome=Outcome.HOME, yes_bid=40, yes_ask=42,
                )
            ]

        async def aclose(self):
            pass

    rt.market_feed = _Feed()
    rt.cfg.kalshi.capture_extra_markets = True
    rt.cfg.kalshi.extra_markets_interval_seconds = 0.0

    orch = Orchestrator(rt, _StaticProvider([]), trade=False)

    async def fake_process(match, snaps, st):
        order.append("process")

    orch.processor.process = fake_process  # type: ignore[assignment]

    await orch._handle(match_factory(match_id="m1", minute=30, period=MatchPeriod.FIRST_HALF))
    # Fire-and-forget: the task exists but hasn't run yet — process was NOT blocked on it.
    assert orch._extra_tasks
    assert order == ["process"]
    await asyncio.gather(*orch._extra_tasks)  # drain the background capture
    # The capture fans out over the whole roadmap series, so it hits the client many times —
    # what matters is the decision ran first and every capture RTT landed strictly after it.
    assert order[0] == "process"
    assert order.count("capture") >= 1
    assert all(step == "capture" for step in order[1:])


async def test_kill_sets_flatten_request(rt):
    orch = Orchestrator(rt, _StaticProvider([]), trade=False)
    rt.cfg.execution.flatten_on_kill = True
    orch.kill("test")
    assert orch._flatten_requested is True
    assert not rt.risk.trading_allowed


async def test_marks_ignore_stale_last_price(rt, match_factory):
    """rt.last_mids feeds unrealized P&L, the daily-loss halt, and position stops —
    a one-sided book must keep its previous mark, not adopt a stale last trade."""
    from wc_kalshi.engine.match_loop import MatchState, TickProcessor
    from wc_kalshi.models.schemas import MarketSnapshot

    proc = TickProcessor(rt, trade=False, persist=False)
    match = match_factory(match_id="m1", minute=10)
    snaps = [
        MarketSnapshot(market_ticker="KX-BOOK", match_id="m1", outcome=Outcome.HOME,
                       yes_bid=40, yes_ask=42),
        MarketSnapshot(market_ticker="KX-STALE", match_id="m1", outcome=Outcome.DRAW,
                       yes_bid=None, yes_ask=None, last_price=95),
    ]
    await proc.process(match, snaps, MatchState("m1"))
    assert rt.last_mids.get("KX-BOOK") == 0.41  # two-sided book marks
    assert "KX-STALE" not in rt.last_mids  # stale print never becomes a mark


async def test_defer_epilogue_moves_whole_book_mark_off_process(rt, match_factory):
    """defer_book_epilogue must move the whole-book mark + snapshots OUT of process() (they
    become a per-poll concern) and into book_epilogue(). Without the flag — the backtest
    path — process() still marks inline, exactly once, as before."""
    from wc_kalshi.engine.match_loop import MatchState, TickProcessor
    from wc_kalshi.models.schemas import MarketSnapshot

    calls = {"n": 0}
    real_uu = rt.risk.update_unrealized

    def counting_uu(v):
        calls["n"] += 1
        return real_uu(v)

    rt.risk.update_unrealized = counting_uu  # type: ignore[method-assign]
    snaps = [MarketSnapshot(market_ticker="KX-BOOK", match_id="m1", outcome=Outcome.HOME,
                            yes_bid=40, yes_ask=42)]
    match = match_factory(match_id="m1", minute=30, period=MatchPeriod.FIRST_HALF)

    # Deferred (live): process() does NOT mark the whole book; book_epilogue() does.
    proc = TickProcessor(rt, trade=False, persist=False, defer_book_epilogue=True)
    await proc.process(match, snaps, MatchState("m1"))
    assert calls["n"] == 0
    proc.book_epilogue()
    assert calls["n"] == 1

    # Not deferred (backtest): process() marks inline, once, unchanged.
    calls["n"] = 0
    proc2 = TickProcessor(rt, trade=False, persist=False)
    await proc2.process(match, snaps, MatchState("m1b"))
    assert calls["n"] == 1


async def test_orchestrator_marks_whole_book_once_per_poll(rt, match_factory):
    """The live loop must mark the whole book + snapshot risk/portfolio ONCE per poll, not
    once per match — that redundancy was O(matches × positions) every tick."""
    from wc_kalshi.models.schemas import MarketSnapshot

    class _Feed:
        async def snapshots_for_match(self, match):
            return [MarketSnapshot(market_ticker=f"KX-{match.match_id}", match_id=match.match_id,
                                   outcome=Outcome.HOME, yes_bid=40, yes_ask=42)]

        async def aclose(self):
            pass

    rt.market_feed = _Feed()
    calls = {"uu": 0, "epi": 0}
    real_uu = rt.risk.update_unrealized

    def counting_uu(v):
        calls["uu"] += 1
        return real_uu(v)

    rt.risk.update_unrealized = counting_uu  # type: ignore[method-assign]

    matches = [match_factory(match_id=f"m{i}", minute=30, period=MatchPeriod.FIRST_HALF)
               for i in range(3)]
    orch = Orchestrator(rt, _StaticProvider(matches), trade=False)
    real_epi = orch.processor.book_epilogue

    def counting_epi():
        calls["epi"] += 1
        return real_epi()

    orch.processor.book_epilogue = counting_epi  # type: ignore[method-assign]
    await orch.run(max_ticks=1)

    assert calls["epi"] == 1  # one epilogue for the whole poll, regardless of match count
    assert calls["uu"] == 1  # ...and the whole-book mark ran once, not three times


async def test_reconcile_books_late_fills_exactly_once(rt):
    """A resting order that fills after placement is booked by reconcile — and only the
    increment each pass (info['booked'] tracks the running total), so repeated reconciles
    can't double-count."""
    ex = _LateFillExecutor()
    rt.executor = ex
    _rest(rt)

    assert await trading.reconcile_resting_orders(rt, persist=False) == 0  # nothing filled yet
    ex.fills = [Fill("c", "m1", "T1", OrderAction.BUY, 10, 50, 0.1)]
    assert await trading.reconcile_resting_orders(rt, persist=False) == 10  # booked once
    assert rt.portfolio.positions["T1"].yes_contracts == 10
    assert await trading.reconcile_resting_orders(rt, persist=False) == 0  # idempotent
    assert rt.portfolio.positions["T1"].yes_contracts == 10
    ex.fills.append(Fill("c", "m1", "T1", OrderAction.BUY, 5, 51, 0.05))
    assert await trading.reconcile_resting_orders(rt, persist=False) == 5  # only the increment
    assert rt.portfolio.positions["T1"].yes_contracts == 15


async def test_sweep_reconciles_late_fill_before_cancelling_stale(rt):
    """Sweep books a fill that landed on a resting order before ageing it out — the late
    fill must not be lost when the stale order is cancelled."""
    ex = _LateFillExecutor()
    rt.executor = ex
    _rest(rt, stale=True)
    ex.fills = [Fill("c", "m1", "T1", OrderAction.BUY, 8, 50, 0.08)]

    canceled = await sweep_resting_orders(rt, timeout_seconds=60)
    assert canceled == 1
    assert "EX-1" in ex.canceled
    assert "EX-1" not in rt.resting_orders  # dropped after cancel
    assert rt.portfolio.positions["T1"].yes_contracts == 8  # late fill booked before the cancel


async def test_paper_executor_reports_no_late_fills():
    """Paper/IOC orders never rest, so the reconciler is a no-op there (backtests unaffected)."""
    from wc_kalshi.execution.paper import PaperExecutor

    ex = PaperExecutor()
    assert await ex.fills_for(
        "x", market_ticker="t", action=OrderAction.BUY, match_id="m",
        client_order_id="c", fallback_price_cents=50,
    ) == []
