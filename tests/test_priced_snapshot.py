"""Phase-3 R5: passing a shared PricedSnapshot to a head must yield IDENTICAL results to
letting the head recompute the backbone itself — across live / 0-0 / scored / finished /
knockout states. The context is a pure accelerator, never a behaviour change."""

import numpy as np

from wc_kalshi.modeling.first_to_score import first_to_score
from wc_kalshi.modeling.inplay import DixonColesInplayModel
from wc_kalshi.modeling.knockout import knockout_breakdown
from wc_kalshi.models.schemas import MatchContext, MatchPeriod, MatchSnapshot, TeamStats


def _snap(minute, period, hs, as_, *, status="live", knockout=False):
    return MatchSnapshot(
        match_id="x", provider="t", home_team="H", away_team="A", minute=minute, period=period,
        home_score=hs, away_score=as_, status=status,
        home=TeamStats(shots=6, shots_on_target=3), away=TeamStats(shots=2, shots_on_target=1),
        context=MatchContext(home_elo=1900.0, away_elo=1600.0, is_knockout=knockout),
    )


_SNAPS = [
    _snap(5, MatchPeriod.FIRST_HALF, 0, 0),          # early, level
    _snap(57, MatchPeriod.SECOND_HALF, 1, 0),        # scored, second half
    _snap(80, MatchPeriod.SECOND_HALF, 2, 2),        # high-scoring, level late
    _snap(70, MatchPeriod.SECOND_HALF, 0, 0, knockout=True),  # knockout, still level
    _snap(90, MatchPeriod.FULL_TIME, 1, 1, status="finished"),  # finished (degenerate)
]


def test_priced_matches_recompute_for_every_head():
    m = DixonColesInplayModel(_cfg())
    for s in _SNAPS:
        pr = m.price(s)

        a, b = m.predict(s), m.predict(s, priced=pr)
        assert a.model_dump(exclude={"ts"}) == b.model_dump(exclude={"ts"}), s.minute
        assert np.array_equal(m.scoreline_matrix(s), m.scoreline_matrix(s, priced=pr))
        assert m.remaining_rates(s) == m.remaining_rates(s, priced=pr)
        assert first_to_score(m, s) == first_to_score(m, s, priced=pr)
        assert knockout_breakdown(m, s) == knockout_breakdown(m, s, priced=pr)


def _cfg():
    from wc_kalshi.config import load_config

    return load_config(load_env=False, use_local=False).model
