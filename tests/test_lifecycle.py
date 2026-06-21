"""Execution lifecycle: kill-switch flatten, resting-order timeout, adaptive polling."""

from datetime import timedelta

import pytest

from wc_kalshi.engine.builders import build_runtime
from wc_kalshi.engine.orchestrator import Orchestrator
from wc_kalshi.engine.trading import flatten_all, sweep_resting_orders
from wc_kalshi.ingestion.football.base import FootballDataProvider
from wc_kalshi.models.schemas import MatchPeriod, OrderAction, Outcome
from wc_kalshi.util import utcnow


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
    """Live for one poll, then the match drops out; fetch_fixture returns its final state."""

    name = "apifootball"

    def __init__(self, final_snap):
        self._final = final_snap
        self.fixture_calls = 0

    async def fetch_live(self):
        return []

    async def fetch_fixture(self, match_id):
        self.fixture_calls += 1
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


async def test_transient_drop_is_not_settled(rt, match_factory):
    """A match that briefly vanishes but isn't actually finished must NOT be settled."""
    still_live = match_factory(match_id="m1", minute=81, period=MatchPeriod.SECOND_HALF)
    orch = Orchestrator(rt, _FinishingProvider(still_live), trade=False)
    orch._live_ids = {"m1"}
    await orch._settle_dropped(current_ids=set())
    assert "m1" not in orch._settled_ids  # not finished -> retry later, no false settle


async def test_kill_sets_flatten_request(rt):
    orch = Orchestrator(rt, _StaticProvider([]), trade=False)
    rt.cfg.execution.flatten_on_kill = True
    orch.kill("test")
    assert orch._flatten_requested is True
    assert not rt.risk.trading_allowed
