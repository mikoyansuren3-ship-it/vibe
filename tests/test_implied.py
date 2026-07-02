"""Market de-vigging."""

import pytest

from wc_kalshi.market.implied import implied_from_markets
from wc_kalshi.models.schemas import MarketSnapshot, Outcome


def _markets(home=(52, 55), draw=(26, 29), away=(18, 21)):
    out = []
    for outcome, (bid, ask) in zip(
        (Outcome.HOME, Outcome.DRAW, Outcome.AWAY), (home, draw, away)
    ):
        out.append(
            MarketSnapshot(
                market_ticker=f"T-{outcome.value}",
                match_id="m1",
                outcome=outcome,
                yes_bid=bid,
                yes_ask=ask,
            )
        )
    return out


@pytest.mark.parametrize("method", ["proportional", "power", "shin"])
def test_devig_sums_to_one(method):
    view = implied_from_markets(_markets(), method=method)
    probs = view.probabilities()
    assert abs(probs.p_home + probs.p_draw + probs.p_away - 1.0) < 1e-6


def test_overround_above_one_and_removed():
    view = implied_from_markets(_markets(), method="proportional")
    assert view.overround > 1.0  # raw mids include vig
    # each implied prob is below its raw mid (vig stripped) for a >1 overround book
    for o, om in view.outcomes.items():
        assert om.implied_prob <= om.mid_prob + 1e-9


def test_favourite_has_highest_probability():
    view = implied_from_markets(_markets(), method="proportional")
    assert view.outcomes[Outcome.HOME].implied_prob > view.outcomes[Outcome.AWAY].implied_prob


def test_full_book_is_complete():
    view = implied_from_markets(_markets(), method="proportional")
    assert view.complete is True


def test_partial_book_is_not_devigged():
    """A missing leg must NOT inflate the others: proportional de-vig over 2 of 3
    legs would force them to sum to 1.0, manufacturing edges. The view keeps the
    raw mids and is flagged incomplete."""
    markets = _markets()[:2]  # only home + draw
    view = implied_from_markets(markets, method="proportional")
    assert view.complete is False
    assert Outcome.AWAY not in view.outcomes
    for om in view.outcomes.values():
        assert om.implied_prob == om.mid_prob  # raw, un-normalized
    assert sum(om.implied_prob for om in view.outcomes.values()) < 1.0


def test_one_sided_leg_with_last_price_does_not_count():
    """A leg quoting only a stale last trade (no two-sided book) contributes nothing
    to de-vig — the whole view degrades to incomplete."""
    markets = _markets()
    markets[2] = MarketSnapshot(
        market_ticker="T-away", match_id="m1", outcome=Outcome.AWAY,
        yes_bid=None, yes_ask=None, last_price=20,
    )
    view = implied_from_markets(markets, method="proportional")
    assert view.complete is False
    assert Outcome.AWAY not in view.outcomes
