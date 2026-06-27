"""Bundle assembly for the web simulator (backtest/export.build_bundle)."""

import json
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


def _persist_match(db, mid, snaps, markets):
    for s in snaps:
        s.match_id = mid
        db.add_match_snapshot(s)
    for m in markets:
        m.match_id = mid
        db.add_market_snapshot(m)


def test_export_live_emits_all_live_matches(cfg, tmp_path):
    from wc_kalshi.backtest.export import export_live
    from wc_kalshi.models.db import Database

    db = Database(f"sqlite:///{tmp_path / 'rec.sqlite3'}")
    # Two live matches (m_b updated more recently) + one finished (excluded).
    _persist_match(db, "m_a", [
        _match(1, MatchPeriod.FIRST_HALF, 0, 0, ts=T0),
        _match(30, MatchPeriod.FIRST_HALF, 1, 0, ts=T0 + timedelta(minutes=30)),
    ], [_mkt(Outcome.HOME, "H", 60, 62, ts=T0 + timedelta(seconds=1))])
    _persist_match(db, "m_b", [
        _match(1, MatchPeriod.FIRST_HALF, 0, 0, ts=T0),
        _match(50, MatchPeriod.SECOND_HALF, 0, 1, ts=T0 + timedelta(minutes=50)),
    ], [_mkt(Outcome.AWAY, "A", 55, 57, ts=T0 + timedelta(seconds=1))])
    _persist_match(db, "m_done", [
        _match(90, MatchPeriod.FULL_TIME, 2, 1, ts=T0, status="finished"),
    ], [])

    doc = export_live(cfg, f"sqlite:///{tmp_path / 'rec.sqlite3'}", str(tmp_path / "out"))
    assert doc["live"] is True
    ids = [b["match_id"] for b in doc["bundles"]]
    assert ids == ["m_b", "m_a"]  # most-recently-updated first; finished excluded
    assert doc["bundle"]["match_id"] == "m_b"  # back-compat singular = first
    assert all(b["live"] is True and b["outcome"] is None for b in doc["bundles"])
    written = json.loads((tmp_path / "out" / "live.json").read_text())
    assert [b["match_id"] for b in written["bundles"]] == ["m_b", "m_a"]


def test_live_bundle_all_markets_board(cfg):
    snaps = [
        _match(1, MatchPeriod.FIRST_HALF, 0, 0, ts=T0),
        _match(40, MatchPeriod.FIRST_HALF, 0, 0, ts=T0 + timedelta(minutes=40)),
    ]
    # (series, ticker, sub, strike, bid, ask) — a priceable + a market-only series.
    quotes = [
        ("KXWCTOTAL", "KX-T-25", "Over 2.5 goals scored", 2.5, 40, 42),
        ("KXWCBTTS", "KX-BTTS", "Both Teams To Score", None, 48, 50),
        ("KXWCCORNERS", "KX-C-8", "8+ corners", 8.0, 50, 52),  # no model price
    ]
    b = build_live_bundle(cfg, "m1", snaps, [], quotes)
    assert b is not None and "all_markets" in b
    by_series = {g["series"]: g for g in b["all_markets"]}
    assert by_series["KXWCTOTAL"]["priceable"] is True
    assert by_series["KXWCTOTAL"]["contracts"][0]["model"] is not None  # priced
    assert by_series["KXWCCORNERS"]["priceable"] is False
    assert by_series["KXWCCORNERS"]["contracts"][0]["model"] is None  # market-only
    assert by_series["KXWCCORNERS"]["contracts"][0]["mid"] == 0.51  # (50+52)/200


def test_parse_correct_score():
    from wc_kalshi.backtest.export import _parse_correct_score
    assert _parse_correct_score("Egypt wins 1-0", "Egypt", "Iran") == (1, 0)
    assert _parse_correct_score("IR Iran wins 2-1", "Egypt", "Iran") == (1, 2)
    assert _parse_correct_score("Draw 1-1", "Egypt", "Iran") == (1, 1)
    assert _parse_correct_score("nonsense", "Egypt", "Iran") is None


def test_export_live_no_live_match(cfg, tmp_path):
    from wc_kalshi.backtest.export import export_live
    from wc_kalshi.models.db import Database

    db = Database(f"sqlite:///{tmp_path / 'rec.sqlite3'}")
    _persist_match(db, "m_done", [
        _match(90, MatchPeriod.FULL_TIME, 2, 1, ts=T0, status="finished"),
    ], [])
    doc = export_live(cfg, f"sqlite:///{tmp_path / 'rec.sqlite3'}", str(tmp_path / "out"))
    assert doc == {"live": False, "bundles": []}
