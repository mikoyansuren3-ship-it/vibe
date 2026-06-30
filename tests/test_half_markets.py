"""Half-market matrices (inplay.half_scoreline_matrices, plan P1.1): the two per-half
matrices must preserve expected goals and convolve back to the full-time matrix, so the
1st/2nd-half markets stay coherent with the full-match board."""

import numpy as np

from wc_kalshi.config import load_config
from wc_kalshi.modeling.derived import total_goals_pmf
from wc_kalshi.modeling.inplay import DixonColesInplayModel
from wc_kalshi.modeling.intensity_profile import half_weights
from wc_kalshi.modeling.poisson import remaining_goal_matrix
from wc_kalshi.models.schemas import MatchContext, MatchPeriod, MatchSnapshot


def _model():
    return DixonColesInplayModel(load_config(load_env=False, use_local=False).model)


def _pre(home_elo=1950.0, away_elo=1750.0):
    return MatchSnapshot(
        match_id="h", provider="x", home_team="H", away_team="A", minute=0,
        period=MatchPeriod.PRE, status="scheduled",
        context=MatchContext(neutral_venue=True, home_elo=home_elo, away_elo=away_elo),
    )


def _exp_home(m):
    return float((np.arange(m.shape[0])[:, None] * m).sum())


def test_half_matrices_preserve_expected_goals():
    model = _model()
    snap = _pre()
    m1, m2 = model.half_scoreline_matrices(snap)
    lam, _mu = model._remaining_rates(snap)
    w1, w2 = half_weights()
    assert abs(_exp_home(m1) - lam * w1) < 1e-6
    assert abs(_exp_home(m2) - lam * w2) < 1e-6
    assert abs(_exp_home(m1) + _exp_home(m2) - lam) < 1e-6  # halves reconstitute the full mean


def test_halves_convolve_to_full_time():
    model = _model()
    snap = _pre(1900, 1800)
    m1, m2 = model.half_scoreline_matrices(snap)
    lam, mu = model._remaining_rates(snap)
    full = remaining_goal_matrix(lam, mu, rho=0.0, max_goals=model.cfg.max_goals)
    # Home marginal: convolving the two half home-pmfs reconstructs the full-time home pmf.
    h1, h2, hf = m1.sum(axis=1), m2.sum(axis=1), full.sum(axis=1)
    assert np.allclose(np.convolve(h1, h2)[:6], hf[:6], atol=1e-4)


def test_second_half_outscores_first():
    model = _model()
    m1, m2 = model.half_scoreline_matrices(_pre(1850, 1820))
    p1, p2 = total_goals_pmf(m1), total_goals_pmf(m2)
    e1 = float((np.arange(len(p1)) * p1).sum())
    e2 = float((np.arange(len(p2)) * p2).sum())
    assert e2 > e1  # HALF1_FRACTION < 0.5 -> more goals in the 2nd half
