"""Real-world-breakage tests: the things that bite in production, not internal
self-consistency. Empty/garbage live context, RNG determinism, and degenerate books.
"""

from wc_kalshi.ingestion.kalshi.sim_market import SimulatedMarket, _stable_seed
from wc_kalshi.models.schemas import (
    MatchContext,
    MatchPeriod,
    MatchSnapshot,
    TeamStats,
)
from wc_kalshi.modeling.inplay import DixonColesInplayModel


def _bare_match(**kw):
    """A live snapshot with NO context at all (the live providers can emit this)."""
    base = dict(
        match_id="x", provider="test", home_team="A", away_team="B",
        minute=30, period=MatchPeriod.FIRST_HALF, home_score=0, away_score=0,
        home=TeamStats(), away=TeamStats(), status="live", context=None,
    )
    base.update(kw)
    return MatchSnapshot(**base)


def test_model_handles_missing_context(model_cfg):
    """An empty MatchContext must not crash the model and must yield a valid simplex."""
    model = DixonColesInplayModel(model_cfg)
    p = model.predict(_bare_match())
    assert abs((p.p_home + p.p_draw + p.p_away) - 1.0) < 1e-6
    assert all(0.0 <= v <= 1.0 for v in (p.p_home, p.p_draw, p.p_away))


def test_model_handles_extreme_garbage_inputs(model_cfg):
    model = DixonColesInplayModel(model_cfg)
    # Absurd xG, both teams a man down, impossible minute — should still be a simplex.
    junk = _bare_match(
        minute=130, home=TeamStats(xg=99.0, red_cards=3), away=TeamStats(xg=0.0, red_cards=2),
        home_score=7, away_score=0,
        context=MatchContext(home_elo=9999.0, away_elo=-50.0),
    )
    p = model.predict(junk)
    assert abs((p.p_home + p.p_draw + p.p_away) - 1.0) < 1e-6
    assert p.p_home > p.p_away  # 7-0 up: home should be the heavy favourite


def test_stable_seed_is_process_independent():
    # Must not depend on PYTHONHASHSEED (unlike the builtin hash()).
    assert _stable_seed("match-1", 42) == _stable_seed("match-1", 42)
    assert _stable_seed("match-1", 42) != _stable_seed("match-2", 42)


def test_simulated_market_is_deterministic():
    m = _bare_match()
    a = SimulatedMarket(match_id="m", seed=7)
    b = SimulatedMarket(match_id="m", seed=7)
    snaps_a = a.snapshots(m)
    snaps_b = b.snapshots(m)
    assert [(s.market_ticker, s.yes_bid, s.yes_ask) for s in snaps_a] == [
        (s.market_ticker, s.yes_bid, s.yes_ask) for s in snaps_b
    ]


def test_empty_book_yields_no_actionable_edge(cfg):
    """A market with no quotes must never produce a tradable signal."""
    from wc_kalshi.edge.detector import EdgeDetector
    from wc_kalshi.market.implied import implied_from_markets
    from wc_kalshi.models.schemas import MarketSnapshot, Outcome

    empty = [
        MarketSnapshot(market_ticker=f"t-{o.value}", match_id="x", outcome=o, yes_bid=None, yes_ask=None)
        for o in (Outcome.HOME, Outcome.DRAW, Outcome.AWAY)
    ]
    view = implied_from_markets(empty, method=cfg.edge.devig_method)
    det = EdgeDetector.from_config(cfg)
    model = DixonColesInplayModel(cfg.model)
    signals = det.evaluate(model.predict(_bare_match()), view) if view else []
    assert all(not s.actionable for s in signals)
