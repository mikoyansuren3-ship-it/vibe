"""Bundle assembly for the web simulator (backtest/export.build_bundle)."""

import asyncio
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


def _engineered_bet_match(db, match_id, *, home_final, away_final):
    """A settled match where the strategy reliably backs Home at 42c (model strongly
    favours Home via a 2100-v-1500 Elo gap): the final score decides the bet's P&L."""
    def snap(minute, period, hs, as_, *, ts, status="live"):
        m = _match(minute, period, hs, as_, ts=ts, status=status)
        m.match_id = match_id
        m.context.home_elo, m.context.away_elo = 2100.0, 1500.0
        return m

    db.add_match_snapshot(snap(5, MatchPeriod.FIRST_HALF, 0, 0, ts=T0))
    db.add_match_snapshot(snap(
        90, MatchPeriod.FULL_TIME, home_final, away_final,
        ts=T0 + timedelta(minutes=90), status="finished",
    ))
    for outcome, suffix, bid, ask in (
        (Outcome.HOME, "H", 40, 42), (Outcome.DRAW, "D", 28, 30), (Outcome.AWAY, "A", 28, 30),
    ):
        m = _mkt(outcome, f"{match_id}-{suffix}", bid, ask, ts=T0 + timedelta(seconds=1))
        m.match_id = match_id
        db.add_market_snapshot(m)


def test_export_attributes_pnl_to_the_right_match(cfg, tmp_path):
    """Bundle P&L must come from the replay's match-KEYED results — the old positional
    zip shifted every match's P&L when the id sets drifted between the two scans."""
    from wc_kalshi.backtest.export import export_bundles
    from wc_kalshi.models.db import Database

    db_path = f"sqlite:///{tmp_path / 'rec.sqlite3'}"
    db = Database(db_path)
    _engineered_bet_match(db, "m_lose", home_final=0, away_final=1)  # Home bet loses
    _engineered_bet_match(db, "m_win", home_final=2, away_final=0)  # Home bet pays

    doc = asyncio.run(export_bundles(cfg, db_path, str(tmp_path / "out")))
    pnl = {
        m["match_id"]: json.loads((tmp_path / "out" / f"{m['match_id']}.json").read_text())["golden"]["pnl"]
        for m in doc["matches"]
    }
    assert pnl["m_win"] > 0 and pnl["m_lose"] < 0


def test_export_attribution_survives_id_order_drift(cfg, tmp_path, monkeypatch):
    """The review's failure mode: match_ids() returns a different order between the
    replay's scan and any later scan (Postgres DISTINCT instability, or the recorder
    settling a match mid-export). Attribution must not depend on that order."""
    from wc_kalshi.backtest.export import export_bundles
    from wc_kalshi.models.db import Database

    db_path = f"sqlite:///{tmp_path / 'rec.sqlite3'}"
    db = Database(db_path)
    _engineered_bet_match(db, "m_lose", home_final=0, away_final=1)
    _engineered_bet_match(db, "m_win", home_final=2, away_final=0)

    real = Database.match_ids
    calls = {"n": 0}

    def drifting(self):
        calls["n"] += 1
        ids = real(self)
        return ids if calls["n"] % 2 else list(reversed(ids))

    monkeypatch.setattr(Database, "match_ids", drifting)
    doc = asyncio.run(export_bundles(cfg, db_path, str(tmp_path / "out2")))
    pnl = {
        m["match_id"]: json.loads((tmp_path / "out2" / f"{m['match_id']}.json").read_text())["golden"]["pnl"]
        for m in doc["matches"]
    }
    assert pnl["m_win"] > 0 and pnl["m_lose"] < 0


def test_export_live_emits_all_live_matches(cfg, tmp_path):
    from wc_kalshi.backtest.export import export_live
    from wc_kalshi.models.db import Database
    from wc_kalshi.util import utcnow

    db = Database(f"sqlite:///{tmp_path / 'rec.sqlite3'}")
    # Live matches must carry RECENT timestamps: export_live excludes anything whose
    # last snapshot is older than the staleness cutoff (never-settled ≠ live).
    t0 = utcnow() - timedelta(minutes=55)
    # Two live matches (m_b updated more recently) + one finished (excluded).
    _persist_match(db, "m_a", [
        _match(1, MatchPeriod.FIRST_HALF, 0, 0, ts=t0),
        _match(30, MatchPeriod.FIRST_HALF, 1, 0, ts=t0 + timedelta(minutes=30)),
    ], [_mkt(Outcome.HOME, "H", 60, 62, ts=t0 + timedelta(seconds=1))])
    _persist_match(db, "m_b", [
        _match(1, MatchPeriod.FIRST_HALF, 0, 0, ts=t0),
        _match(50, MatchPeriod.SECOND_HALF, 0, 1, ts=t0 + timedelta(minutes=50)),
    ], [_mkt(Outcome.AWAY, "A", 55, 57, ts=t0 + timedelta(seconds=1))])
    _persist_match(db, "m_done", [
        _match(90, MatchPeriod.FULL_TIME, 2, 1, ts=t0, status="finished"),
    ], [])

    doc = asyncio.run(export_live(cfg, f"sqlite:///{tmp_path / 'rec.sqlite3'}", str(tmp_path / "out")))
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
    doc = asyncio.run(export_live(cfg, f"sqlite:///{tmp_path / 'rec.sqlite3'}", str(tmp_path / "out")))
    assert doc["live"] is False
    assert doc["bundles"] == []
    assert "generated_at" in doc  # staleness heartbeat for the web's "live feed offline" state
    assert "upcoming" in doc  # projections present even when nothing is live


def test_export_live_excludes_stale_never_settled_match(cfg, tmp_path):
    """A match that never got its FT snapshot (recorder crash / settlement gave up)
    keeps a live-looking last tick forever — it must NOT be published as in-progress
    for the rest of the tournament."""
    from wc_kalshi.backtest.export import export_live
    from wc_kalshi.models.db import Database
    from wc_kalshi.util import utcnow

    db = Database(f"sqlite:///{tmp_path / 'rec.sqlite3'}")
    stale_t0 = utcnow() - timedelta(hours=30)
    _persist_match(db, "m_stale", [
        _match(1, MatchPeriod.FIRST_HALF, 0, 0, ts=stale_t0),
        _match(70, MatchPeriod.SECOND_HALF, 1, 0, ts=stale_t0 + timedelta(minutes=70)),
    ], [])
    fresh_t0 = utcnow() - timedelta(minutes=40)
    _persist_match(db, "m_fresh", [
        _match(1, MatchPeriod.FIRST_HALF, 0, 0, ts=fresh_t0),
        _match(35, MatchPeriod.FIRST_HALF, 0, 0, ts=fresh_t0 + timedelta(minutes=35)),
    ], [_mkt(Outcome.HOME, "H", 50, 52, ts=fresh_t0 + timedelta(seconds=1))])

    doc = asyncio.run(export_live(cfg, f"sqlite:///{tmp_path / 'rec.sqlite3'}", str(tmp_path / "out")))
    ids = [b["match_id"] for b in doc["bundles"]]
    assert ids == ["m_fresh"]  # the abandoned recording is not "live"


def _pre(match_id="up1", *, home_elo=1900.0, away_elo=1700.0, kickoff=None):
    return MatchSnapshot(
        match_id=match_id, provider="test", home_team="Home", away_team="Away",
        minute=0, period=MatchPeriod.PRE, status="scheduled",
        context=MatchContext(neutral_venue=True, home_elo=home_elo, away_elo=away_elo, kickoff=kickoff),
    )


def test_build_upcoming_bundle_full_board(cfg):
    from wc_kalshi.backtest.export import build_upcoming_bundle

    b = build_upcoming_bundle(cfg, _pre(home_elo=1950.0, away_elo=1650.0, kickoff=T0))
    assert b["upcoming"] is True and b["live"] is False and b["outcome"] is None
    assert b["n_ticks"] == 0 and b["ticks"] == []  # no per-tick history for a future game
    assert b["kickoff"] == T0.isoformat()
    # The full model board: 1X2 + every scoreline-derived market.
    series = {g["series"] for g in b["all_markets"]}
    assert {"KXWCGAME", "KXWCTOTAL", "KXWCBTTS", "KXWCSPREAD", "KXWCTEAMTOTAL", "KXWCSCORE"} <= series
    game = next(g for g in b["all_markets"] if g["series"] == "KXWCGAME")
    assert abs(sum(c["model"] for c in game["contracts"]) - 1.0) < 1e-6  # 1X2 normalized
    assert b["model"][0] > b["model"][2]  # home favoured (higher Elo)
    # No market open → every contract is model-only (no bid/ask/mid).
    assert all(c["bid"] is None and c["mid"] is None for g in b["all_markets"] for c in g["contracts"])
    # Correct-score group is the top-N most likely scorelines, descending.
    scores = [c["model"] for c in next(g for g in b["all_markets"] if g["series"] == "KXWCSCORE")["contracts"]]
    assert scores == sorted(scores, reverse=True) and len(scores) == 6


def test_build_upcoming_bundle_overlays_quotes(cfg):
    from wc_kalshi.backtest.export import build_upcoming_bundle

    quotes = [
        ("KXWCGAME", "g-home", "Home", None, 55, 57),       # 1X2 leg → overlay onto home
        ("KXWCTOTAL", "t-25", "Over 2.5", 2.5, 48, 50),     # priceable → model + market
        ("KXWCCORNERS", "c", "9+ corners", 9.0, 30, 33),    # market-only, model can't price
    ]
    b = build_upcoming_bundle(cfg, _pre(home_elo=1850.0, away_elo=1800.0), quotes)
    by = {g["series"]: g for g in b["all_markets"]}
    home_c = next(c for c in by["KXWCGAME"]["contracts"] if c["label"] == "Home")
    assert home_c["mid"] == 0.56 and home_c["model"] is not None  # (55+57)/200 overlaid
    tot = next(c for c in by["KXWCTOTAL"]["contracts"] if c["strike"] == 2.5)
    assert tot["mid"] == 0.49 and tot["model"] is not None
    # Real market-only series the model can't price is appended, not hidden.
    assert "KXWCCORNERS" in by and by["KXWCCORNERS"]["priceable"] is False
    assert by["KXWCCORNERS"]["contracts"][0]["model"] is None


def test_build_upcoming_bundle_surfaces_offladder_quotes(cfg):
    from wc_kalshi.backtest.export import build_upcoming_bundle

    # Captured strikes OFF the canonical model ladder must still appear (model-priced),
    # not be silently dropped — otherwise real pre-off market data is hidden.
    quotes = [
        ("KXWCSPREAD", "s35", "Home wins by more than 3.5", 3.5, 20, 23),  # spread ladder maxes 2.5
        ("KXWCTOTAL", "t65", "Over 6.5 goals", 6.5, 8, 11),                # total ladder maxes 5.5
    ]
    b = build_upcoming_bundle(cfg, _pre(home_elo=2050.0, away_elo=1650.0), quotes)
    by = {g["series"]: g for g in b["all_markets"]}
    s35 = next((c for c in by["KXWCSPREAD"]["contracts"] if c["strike"] == 3.5), None)
    t65 = next((c for c in by["KXWCTOTAL"]["contracts"] if c["strike"] == 6.5), None)
    assert s35 is not None and t65 is not None  # off-ladder strikes surfaced
    assert s35["mid"] == round((20 + 23) / 200, 4)  # carries the captured market
    assert s35["model"] is not None and t65["model"] is not None  # and a model price


def _ko(round_label="Round of 16", home_elo=2000.0, away_elo=1760.0):
    return MatchSnapshot(
        match_id="ko1", provider="test", home_team="Home", away_team="Away", minute=0,
        period=MatchPeriod.PRE, status="scheduled",
        context=MatchContext(neutral_venue=True, home_elo=home_elo, away_elo=away_elo,
                             round=round_label, is_knockout=True),
    )


def test_build_upcoming_bundle_knockout_board(cfg):
    from wc_kalshi.backtest.export import build_upcoming_bundle

    b = build_upcoming_bundle(cfg, _ko())
    assert b["is_knockout"] is True and b["round"] == "Round of 16"
    assert abs(sum(b["advance"]) - 1.0) < 1e-6
    by = {g["series"]: g for g in b["all_markets"]}
    # Knockout markets present AND the regulation board kept (1X2 etc. settle on 90').
    assert {"KXWCADVANCE", "KXWCMOV", "KXWCTOET", "KXWCTOPENS", "KXWCETSCORE", "KXWCGAME"} <= set(by)
    adv = by["KXWCADVANCE"]["contracts"]
    assert len(adv) == 2 and abs(sum(c["model"] for c in adv) - 1.0) < 1e-6
    assert len(by["KXWCMOV"]["contracts"]) == 6  # win in reg/ET/pens per team
    # Method of advancement (home legs) decomposes into the home advance probability.
    home_mov = sum(c["model"] for c in by["KXWCMOV"]["contracts"][:3])
    assert abs(home_mov - adv[0]["model"]) < 1e-3
    # Knockout markets lead the board.
    assert b["all_markets"][0]["series"] == "KXWCADVANCE"


def test_build_upcoming_bundle_group_stage_has_no_knockout(cfg):
    from wc_kalshi.backtest.export import build_upcoming_bundle

    b = build_upcoming_bundle(cfg, _pre())  # _pre has no round / is_knockout
    assert "is_knockout" not in b and "advance" not in b
    assert not any(g["series"] == "KXWCADVANCE" for g in b["all_markets"])


def test_build_upcoming_bundle_advance_market_overlay(cfg):
    from wc_kalshi.backtest.export import build_upcoming_bundle

    quotes = [("KXWCADVANCE", "adv-h", "Home advances", None, 60, 63)]
    b = build_upcoming_bundle(cfg, _ko(round_label="Quarter-finals"), quotes)
    by = {g["series"]: g for g in b["all_markets"]}
    home_adv = next(c for c in by["KXWCADVANCE"]["contracts"] if c["label"] == "Home advances")
    assert home_adv["mid"] == round((60 + 63) / 200, 4)  # captured market overlaid
    assert home_adv["model"] is not None  # alongside the model price


def test_build_upcoming_bundle_half_markets(cfg):
    from wc_kalshi.backtest.export import build_upcoming_bundle

    b = build_upcoming_bundle(cfg, _pre(home_elo=2000.0, away_elo=1700.0))
    by = {g["series"]: g for g in b["all_markets"]}
    assert {"KXWC1H", "KXWC1HTOTAL", "KXWC1HBTTS", "KXWC2H", "KXWC2HTOTAL", "KXWC2HBTTS"} <= set(by)
    # A half result is a valid 1X2 (sums to 1).
    assert abs(sum(c["model"] for c in by["KXWC1H"]["contracts"]) - 1.0) < 1e-6
    # A half over-line is below the full-match one (fewer goals in a half) ...
    h1 = next(c["model"] for c in by["KXWC1HTOTAL"]["contracts"] if c["strike"] == 0.5)
    full = next(c["model"] for c in by["KXWCTOTAL"]["contracts"] if c["strike"] == 0.5)
    assert h1 < full
    # ... and the 2nd half outscores the 1st (HALF1_FRACTION < 0.5).
    h2 = next(c["model"] for c in by["KXWC2HTOTAL"]["contracts"] if c["strike"] == 0.5)
    assert h2 > h1


def test_export_live_includes_upcoming(cfg, tmp_path):
    from wc_kalshi.backtest.export import export_live
    from wc_kalshi.models.db import Database

    db = Database(f"sqlite:///{tmp_path / 'rec.sqlite3'}")
    _persist_match(db, "m_done", [
        _match(90, MatchPeriod.FULL_TIME, 2, 1, ts=T0, status="finished"),
    ], [])
    doc = asyncio.run(export_live(cfg, f"sqlite:///{tmp_path / 'rec.sqlite3'}", str(tmp_path / "out")))
    assert isinstance(doc["upcoming"], list) and doc["upcoming"]  # sim provider projects fixtures
    up = doc["upcoming"][0]
    assert up["upcoming"] is True and up["all_markets"]
    # Kickoff-sorted (soonest first; undated last).
    kos = [b.get("kickoff") for b in doc["upcoming"]]
    assert kos == sorted(kos, key=lambda k: (k is None, k or ""))
    written = json.loads((tmp_path / "out" / "live.json").read_text())
    assert len(written["upcoming"]) == len(doc["upcoming"])


def test_latest_match_snapshot_meta_probes_last_row_per_match(tmp_path):
    """The SQL probe returns the LAST snapshot's (period, ts) per match from promoted columns
    only — the cheap basis for finding live matches without loading every history."""
    from wc_kalshi.models.db import Database
    from wc_kalshi.util import utcnow

    db = Database(f"sqlite:///{tmp_path / 'p.sqlite3'}")
    t0 = utcnow() - timedelta(minutes=60)
    _persist_match(db, "m1", [
        _match(1, MatchPeriod.FIRST_HALF, 0, 0, ts=t0),
        _match(80, MatchPeriod.SECOND_HALF, 2, 0, ts=t0 + timedelta(minutes=80)),
    ], [])
    _persist_match(db, "m2", [
        _match(90, MatchPeriod.FULL_TIME, 1, 1, ts=t0 + timedelta(minutes=95), status="finished"),
    ], [])

    meta = {mid: period for mid, period, _ts in db.latest_match_snapshot_meta()}
    assert set(meta) == {"m1", "m2"}
    assert meta["m1"] == MatchPeriod.SECOND_HALF.value  # the LAST row's period, not the first
    assert meta["m2"] == MatchPeriod.FULL_TIME.value


def test_export_live_loads_only_live_match_histories(cfg, tmp_path, monkeypatch):
    """The probe keeps export O(live): iter_match_snapshots runs only for matches the cheap
    (period, ts) probe flags live — never for finished or stale ones."""
    from wc_kalshi.backtest.export import export_live
    from wc_kalshi.models.db import Database
    from wc_kalshi.util import utcnow

    db_url = f"sqlite:///{tmp_path / 'rec.sqlite3'}"
    db = Database(db_url)
    t0 = utcnow() - timedelta(minutes=40)
    _persist_match(db, "live1", [_match(30, MatchPeriod.FIRST_HALF, 0, 0, ts=t0)],
                   [_mkt(Outcome.HOME, "H", 60, 62, ts=t0)])
    _persist_match(db, "done1",
                   [_match(90, MatchPeriod.FULL_TIME, 2, 1, ts=t0, status="finished")], [])
    old = utcnow() - timedelta(hours=6)  # live-looking last snapshot but past the 3h cutoff
    _persist_match(db, "stale1", [_match(30, MatchPeriod.FIRST_HALF, 0, 0, ts=old)],
                   [_mkt(Outcome.HOME, "H", 60, 62, ts=old)])

    loaded: list[str] = []
    real = Database.iter_match_snapshots

    def spy(self, mid):
        loaded.append(mid)
        return real(self, mid)

    monkeypatch.setattr(Database, "iter_match_snapshots", spy)
    doc = asyncio.run(export_live(cfg, db_url, str(tmp_path / "out")))

    assert [b["match_id"] for b in doc["bundles"]] == ["live1"]
    assert loaded == ["live1"]  # finished + stale filtered by the probe, never loaded
