"""Market-as-prior shrinkage (edge.market_pool_weight): log-opinion pool of the model
with the de-vigged market. w=1 is a no-op (pure model); w=0 defers fully to the market."""

from wc_kalshi.edge.detector import EdgeDetector, market_pooled
from wc_kalshi.market.implied import implied_from_markets
from wc_kalshi.models.schemas import MarketSnapshot, Outcome, Probabilities


def _market():
    snaps = [
        MarketSnapshot(market_ticker="H", match_id="m", outcome=Outcome.HOME, yes_bid=40, yes_ask=42),
        MarketSnapshot(market_ticker="D", match_id="m", outcome=Outcome.DRAW, yes_bid=28, yes_ask=30),
        MarketSnapshot(market_ticker="A", match_id="m", outcome=Outcome.AWAY, yes_bid=28, yes_ask=30),
    ]
    return implied_from_markets(snaps)


def _model():
    return Probabilities(match_id="m", p_home=0.6, p_draw=0.25, p_away=0.15, source="model")


def test_pool_w1_is_identity():
    p = market_pooled(_model(), _market(), 1.0)
    assert p[Outcome.HOME] == 0.6 and p[Outcome.DRAW] == 0.25 and p[Outcome.AWAY] == 0.15


def test_pool_w0_is_market_and_normalized():
    mv = _market()
    p = market_pooled(_model(), mv, 0.0)
    assert abs(p[Outcome.HOME] - mv.outcomes[Outcome.HOME].implied_prob) < 1e-9
    assert abs(sum(p.values()) - 1.0) < 1e-9


def test_pool_skips_when_book_incomplete():
    # Only home + away quoted (no draw) -> no coherent 1X2 vector -> keep the model.
    snaps = [
        MarketSnapshot(market_ticker="H", match_id="m", outcome=Outcome.HOME, yes_bid=40, yes_ask=42),
        MarketSnapshot(market_ticker="A", match_id="m", outcome=Outcome.AWAY, yes_bid=40, yes_ask=42),
    ]
    p = market_pooled(_model(), implied_from_markets(snaps), 0.2)
    assert p[Outcome.HOME] == 0.6  # untouched


def test_detector_w0_finds_no_edge():
    mv, model = _market(), _model()
    sigs0 = EdgeDetector(market_pool_weight=0.0).evaluate(model, mv)
    assert all(abs(s.raw_edge) < 1e-9 for s in sigs0)  # shrunk to market -> nothing to trade
    sigs1 = EdgeDetector(market_pool_weight=1.0).evaluate(model, mv)
    assert any(abs(s.raw_edge) > 0.05 for s in sigs1)  # pure model diverges from the market


def test_detector_default_weight_is_pure_model():
    assert EdgeDetector().market_pool_weight == 1.0
