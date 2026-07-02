"""Edge detection: direction, cost subtraction, actionability thresholds."""

from wc_kalshi.edge.detector import EdgeDetector
from wc_kalshi.market.implied import implied_from_markets
from wc_kalshi.models.schemas import MarketSnapshot, OrderAction, Outcome, Probabilities


def _view(home=(49, 51), draw=(26, 29), away=(22, 25)):
    snaps = []
    for o, (bid, ask) in zip((Outcome.HOME, Outcome.DRAW, Outcome.AWAY), (home, draw, away)):
        snaps.append(MarketSnapshot(market_ticker=f"T-{o.value}", match_id="m1", outcome=o, yes_bid=bid, yes_ask=ask))
    return implied_from_markets(snaps, method="proportional")


def _probs(ph, pd, pa):
    return Probabilities(match_id="m1", p_home=ph, p_draw=pd, p_away=pa).normalized()


def test_model_above_market_triggers_buy():
    det = EdgeDetector(min_edge=0.03, min_edge_after_costs=0.01)
    view = _view()
    sig = next(s for s in det.evaluate(_probs(0.70, 0.20, 0.10), view) if s.outcome is Outcome.HOME)
    assert sig.raw_edge > 0
    assert sig.action is OrderAction.BUY
    assert sig.actionable


def test_model_below_market_triggers_sell():
    det = EdgeDetector(min_edge=0.03, min_edge_after_costs=0.01)
    view = _view()
    sig = next(s for s in det.evaluate(_probs(0.20, 0.20, 0.60), view) if s.outcome is Outcome.HOME)
    assert sig.raw_edge < 0
    assert sig.action is OrderAction.SELL
    assert sig.actionable


def test_costs_reduce_net_edge_below_raw():
    det = EdgeDetector(min_edge=0.0, min_edge_after_costs=0.0)
    view = _view()
    sig = next(s for s in det.evaluate(_probs(0.62, 0.20, 0.18), view) if s.outcome is Outcome.HOME)
    assert sig.net_edge < abs(sig.raw_edge)  # fees+spread+slippage subtracted
    assert sig.est_cost > 0


def test_small_edge_not_actionable():
    det = EdgeDetector(min_edge=0.05, min_edge_after_costs=0.03)
    view = _view()
    # model ~ market => tiny edge
    sigs = det.evaluate(_probs(0.50, 0.27, 0.23), view)
    assert all(not s.actionable for s in sigs)


def test_price_outside_band_not_actionable():
    det = EdgeDetector(min_edge=0.01, min_edge_after_costs=0.0, min_price=0.10, max_price=0.90)
    # away market priced at 4/6c (below band); model loves it
    view = _view(away=(4, 6))
    sig = next(s for s in det.evaluate(_probs(0.10, 0.20, 0.70), view) if s.outcome is Outcome.AWAY)
    assert not sig.actionable


def test_incomplete_book_produces_no_signals():
    """With a leg's book missing, the view carries raw (un-de-vigged) mids — no
    coherent market to compare against, so the detector must not act at all."""
    det = EdgeDetector(min_edge=0.01, min_edge_after_costs=0.0)
    snaps = [
        MarketSnapshot(market_ticker=f"T-{o.value}", match_id="m1", outcome=o, yes_bid=b, yes_ask=a)
        for o, (b, a) in (((Outcome.HOME), (49, 51)), ((Outcome.DRAW), (26, 29)))
    ]
    view = implied_from_markets(snaps, method="proportional")
    assert det.evaluate(_probs(0.70, 0.20, 0.10), view) == []
