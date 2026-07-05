"""Backtest harness: runs offline, populates metrics, deterministic."""

from datetime import datetime, timedelta, timezone

from wc_kalshi.backtest.replay import Backtester
from wc_kalshi.engine.match_loop import CALIBRATION_CHECKPOINTS
from wc_kalshi.models.schemas import (
    MarketSnapshot,
    MatchContext,
    MatchPeriod,
    MatchSnapshot,
    Outcome,
    TeamStats,
)


async def test_backtest_runs_and_reports(cfg):
    bt = Backtester(cfg, trade=True)
    res = await bt.run_synthetic(n_matches=12, seed0=0)
    assert res.n_matches == 12
    # Calibration now pools in-play predictions across the match (checkpoints) plus
    # every traded prediction — many points per match, not just the minute-1 prior.
    assert res.calibration["n"] >= len(CALIBRATION_CHECKPOINTS) * 12
    assert res.starting_bankroll == cfg.risk.starting_bankroll
    assert "BACKTEST REPORT" in res.report()
    await bt.aclose()


async def test_backtest_is_deterministic(cfg):
    bt1 = Backtester(cfg, trade=True)
    r1 = await bt1.run_synthetic(n_matches=8, seed0=100)
    await bt1.aclose()
    bt2 = Backtester(cfg, trade=True)
    r2 = await bt2.run_synthetic(n_matches=8, seed0=100)
    await bt2.aclose()
    assert abs(r1.realized_pnl - r2.realized_pnl) < 1e-6
    assert r1.n_fills == r2.n_fills


async def test_no_trade_mode_places_nothing(cfg):
    bt = Backtester(cfg, trade=False)
    res = await bt.run_synthetic(n_matches=10, seed0=3)
    assert res.n_fills == 0
    assert res.fees_paid == 0.0
    # No trades -> exactly the checkpoint predictions per settled match accrue.
    assert res.calibration["n"] == len(CALIBRATION_CHECKPOINTS) * 10
    await bt.aclose()


T0 = datetime(2026, 6, 25, 20, 0, tzinfo=timezone.utc)


def _losing_match(db, match_id: str, t0):
    """One settled match engineered to take a large losing bet: the model strongly
    favours Home (Elo 2100 v 1500) while the market prices Home at 42c, and Away wins."""

    def snap(minute, period, hs, as_, *, ts, status="live"):
        return MatchSnapshot(
            match_id=match_id, provider="test", ts=ts, home_team="Home", away_team="Away",
            minute=minute, period=period, home_score=hs, away_score=as_, status=status,
            home=TeamStats(), away=TeamStats(),
            context=MatchContext(neutral_venue=True, home_elo=2100.0, away_elo=1500.0),
        )

    db.add_match_snapshot(snap(5, MatchPeriod.FIRST_HALF, 0, 0, ts=t0))
    db.add_match_snapshot(
        snap(90, MatchPeriod.FULL_TIME, 0, 1, ts=t0 + timedelta(minutes=90), status="finished")
    )
    for outcome, suffix, bid, ask in (
        (Outcome.HOME, "H", 40, 42),
        (Outcome.DRAW, "D", 28, 30),
        (Outcome.AWAY, "A", 28, 30),
    ):
        db.add_market_snapshot(
            MarketSnapshot(
                market_ticker=f"KX-{match_id}-{suffix}", match_id=match_id, outcome=outcome,
                ts=t0 + timedelta(seconds=1), yes_bid=bid, yes_ask=ask,
            )
        )


async def test_replay_evaluates_full_sample_despite_daily_loss_limit(cfg, tmp_db):
    """run_replay must neutralize the daily-loss halt (a LIVE guardrail): a replay
    compresses the session into one wall-clock day, so with the halt active match 1's
    realized loss would halt trading and silently censor every later match."""
    cfg.risk.max_daily_loss = 5.0  # far below one engineered losing bet (~$40+)
    _losing_match(tmp_db, "r1", T0)
    _losing_match(tmp_db, "r2", T0 + timedelta(hours=3))

    bt = Backtester(cfg, trade=True)
    res = await bt.run_replay(tmp_db)

    assert res.n_matches == 2
    assert not bt.rt.risk.halted
    assert res.n_fills == 2  # one losing fill per match — match 2 was NOT censored
    assert all(p < 0 for p in res.per_match_pnl)
    # P&L is also keyed by match_id (what export attribution must consume).
    assert set(res.pnl_by_match) == {"r1", "r2"}
    assert list(res.pnl_by_match.values()) == res.per_match_pnl
    await bt.aclose()


def test_bucket_market_by_tick_matches_naive_reference():
    """The two-pointer bucketer must be byte-for-byte equivalent to the old O(N×M) filter —
    including the fiddly cases: duplicate match timestamps (empty buckets), a snap exactly on
    a bucket boundary (belongs to the NEXT tick), and snaps before the first match snap
    (dropped). Both take ts-sorted inputs; the function only reads `.ts`."""
    import random
    from types import SimpleNamespace

    from wc_kalshi.backtest.replay import _bucket_market_by_tick

    def _naive(match_snaps, market_snaps):
        out = []
        for i, match in enumerate(match_snaps):
            lo = match.ts
            hi = match_snaps[i + 1].ts if i + 1 < len(match_snaps) else None
            bucket = [s for s in market_snaps if s.ts >= lo and (hi is None or s.ts < hi)]
            out.append((match, bucket))
        return out

    def _ns(ts_list):
        return [SimpleNamespace(ts=t) for t in ts_list]

    def _shape(pairs):
        return [(m.ts, [s.ts for s in b]) for m, b in pairs]

    cases = [
        ([10, 20, 30], [10, 15, 20, 25, 35]),  # normal, plus a snap past the last bucket-open
        ([10, 10, 20], [8, 10, 12, 20]),        # duplicate match ts (empty bucket) + pre-first snap
        ([10, 20], [10, 20]),                    # boundary: a snap AT hi belongs to the next tick
        ([10], []),                              # no market snaps
        ([], [10, 20]),                          # no match snaps
        ([5, 5, 5], [5, 5]),                     # all-duplicate match ts
    ]
    for mts, kts in cases:
        matches, markets = _ns(mts), _ns(kts)
        assert _shape(_bucket_market_by_tick(matches, markets)) == _shape(_naive(matches, markets)), (mts, kts)

    rng = random.Random(0)
    for _ in range(200):
        mts = sorted(rng.randint(0, 20) for _ in range(rng.randint(0, 8)))
        kts = sorted(rng.randint(0, 22) for _ in range(rng.randint(0, 15)))
        matches, markets = _ns(mts), _ns(kts)
        assert _shape(_bucket_market_by_tick(matches, markets)) == _shape(_naive(matches, markets)), (mts, kts)
