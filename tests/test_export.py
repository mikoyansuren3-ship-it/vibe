"""Bundle assembly for the web simulator (backtest/export.build_bundle)."""

from datetime import datetime, timedelta, timezone

from wc_kalshi.backtest.export import build_bundle, build_live_bundle
from wc_kalshi.models.schemas import (
    MarketSnapshot,
    MatchContext,
    MatchPeriod,
    MatchSnapshot,
    Outcome,
    TeamStats,
)

T0 = datetime(2026, 6, 25, 20, 0, tzinfo=timezone.utc)


def _match(minute, period, hs, as_, *, ts, status="live"):
    return MatchSnapshot(
        match_id="m1", provider="test", ts=ts, home_team="Home", away_team="Away",
        minute=minute, period=period, home_score=hs, away_score=as_, status=status,
        home=TeamStats(shots=4, shots_on_target=2), away=TeamStats(shots=3, shots_on_target=1),
        context=MatchContext(neutral_venue=True, home_elo=1800.0, away_elo=1700.0),
    )


def _mkt(outcome, suffix, bid, ask, *, ts):
    return MarketSnapshot(
        market_ticker=f"KX-{suffix}", match_id="m1", outcome=outcome, ts=ts,
        yes_bid=bid, yes_ask=ask,
    )


def _bundle(cfg):
    snaps = [
        _match(1, MatchPeriod.FIRST_HALF, 0, 0, ts=T0),
        _match(80, MatchPeriod.SECOND_HALF, 2, 0, ts=T0 + timedelta(minutes=80)),
        _match(90, MatchPeriod.FULL_TIME, 2, 1, ts=T0 + timedelta(minutes=95), status="finished"),
    ]
    markets = [
        _mkt(Outcome.HOME, "H", 40, 42, ts=T0 + timedelta(seconds=1)),
        _mkt(Outcome.DRAW, "D", 28, 30, ts=T0 + timedelta(seconds=1)),
        _mkt(Outcome.AWAY, "A", 28, 30, ts=T0 + timedelta(seconds=1)),
        _mkt(Outcome.HOME, "H", 70, 72, ts=T0 + timedelta(minutes=80, seconds=1)),
    ]
    return build_bundle(cfg, "m1", snaps, markets, golden_fills=[], per_match_pnl=0.0, kelly_factor=0.8)


def test_bundle_basic_shape(cfg):
    b = _bundle(cfg)
    assert b is not None
    assert b["match_id"] == "m1"
    assert b["outcome"] == "H" and b["final_score"] == [2, 1]  # 2-1 at 90' => home win
    assert b["n_ticks"] == 3 and len(b["ticks"]) == 3
    # model is a normalized H/D/A triple
    m = b["ticks"][0]["model"]
    assert len(m) == 3 and abs(sum(m) - 1.0) < 1e-6


def test_bundle_market_compaction_and_carry(cfg):
    b = _bundle(cfg)
    # tickers hoisted to top level, dropped from per-tick payload
    assert set(b["tickers"]) == {"home", "draw", "away"}
    assert b["ticks"][0]["markets"]["home"] == [40, 42]  # [bid, ask] cents
    # only home re-quoted at min80; draw/away carried forward from the open
    assert b["ticks"][1]["markets"]["home"] == [70, 72]
    assert b["ticks"][1]["markets"]["draw"] == [28, 30]
    # pre-off reference = earliest mid per outcome (home (40+42)/200 = 0.41)
    assert abs(b["preoff"]["home"] - 0.41) < 1e-9


def test_unsettled_match_returns_none(cfg):
    live_only = [_match(30, MatchPeriod.FIRST_HALF, 0, 0, ts=T0)]
    assert build_bundle(cfg, "m1", live_only, [], golden_fills=[], per_match_pnl=0.0, kelly_factor=1.0) is None


def test_live_bundle_for_inprogress_match(cfg):
    snaps = [
        _match(1, MatchPeriod.FIRST_HALF, 0, 0, ts=T0),
        _match(55, MatchPeriod.SECOND_HALF, 1, 0, ts=T0 + timedelta(minutes=55)),
    ]
    markets = [_mkt(Outcome.HOME, "H", 60, 62, ts=T0 + timedelta(seconds=1))]
    b = build_live_bundle(cfg, "m1", snaps, markets)
    assert b is not None
    assert b["live"] is True and b["outcome"] is None
    assert b["minute"] == 55 and b["final_score"] == [1, 0]  # current (not final) score
    assert b["golden"]["n_fills"] == 0  # no settled fills for a live match


def test_live_bundle_none_when_finished(cfg):
    finished = [_match(90, MatchPeriod.FULL_TIME, 2, 1, ts=T0, status="finished")]
    assert build_live_bundle(cfg, "m1", finished, []) is None
