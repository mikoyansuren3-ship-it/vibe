"""Scoreline-derived market calculators (Tier 1) + the model's scoreline matrix."""

import numpy as np

from wc_kalshi.modeling import derived
from wc_kalshi.modeling.inplay import DixonColesInplayModel


def _collapse_1x2(m):
    """Sum a (possibly non-square) final-score matrix into (home, draw, away)."""
    n_h, n_a = m.shape
    ph = sum(m[i, j] for i in range(n_h) for j in range(n_a) if i > j)
    pd = sum(m[i, j] for i in range(n_h) for j in range(n_a) if i == j)
    pa = sum(m[i, j] for i in range(n_h) for j in range(n_a) if i < j)
    return ph, pd, pa


def _known_matrix():
    # A tiny 3x3 joint score matrix (home rows, away cols), sums to 1.
    return np.array([
        [0.30, 0.10, 0.05],   # home 0 : away 0/1/2
        [0.15, 0.12, 0.03],   # home 1
        [0.10, 0.05, 0.10],   # home 2
    ])


def test_total_over_and_under():
    m = _known_matrix()
    # totals: 0:0.30 | 1:0.10+0.15 | 2:0.05+0.12+0.10 | 3:0.03+0.05 | 4:0.10
    assert abs(derived.prob_total_over(m, 0.5) - (1 - 0.30)) < 1e-9
    assert abs(derived.prob_total_over(m, 2.5) - (0.08 + 0.10)) < 1e-9
    assert abs(derived.prob_total_under(m, 2.5) - (1 - 0.18)) < 1e-9


def test_btts_and_correct_score():
    m = _known_matrix()
    assert abs(derived.prob_btts(m) - m[1:, 1:].sum()) < 1e-9
    assert abs(derived.prob_btts(m) - (0.12 + 0.03 + 0.05 + 0.10)) < 1e-9
    assert derived.prob_correct_score(m, 2, 2) == 0.10
    assert derived.prob_correct_score(m, 9, 9) == 0.0


def test_team_total_and_spread_and_margin():
    m = _known_matrix()
    # home marginal: [0.45, 0.30, 0.25] -> P(home>0.5)=0.55
    assert abs(derived.prob_team_total_over(m, "home", 0.5) - 0.55) < 1e-9
    # home wins by >0.5 => margin>=1: cells (1,0),(2,0),(2,1) = 0.15+0.10+0.05
    assert abs(derived.prob_spread(m, "home", 0.5) - 0.30) < 1e-9
    # away wins by >0.5 => margin(home-away) <= -1: (0,1),(0,2),(1,2) = 0.10+0.05+0.03
    assert abs(derived.prob_spread(m, "away", 0.5) - 0.18) < 1e-9
    assert abs(derived.prob_margin(m, 0) - (0.30 + 0.12 + 0.10)) < 1e-9  # draws diagonal


def test_probabilities_partition():
    m = _known_matrix()
    # over + under = 1; home-win + draw + away-win = 1
    assert abs(derived.prob_total_over(m, 1.5) + derived.prob_total_under(m, 1.5) - 1.0) < 1e-9
    hw = derived.prob_spread(m, "home", 0.5)
    aw = derived.prob_spread(m, "away", 0.5)
    dr = derived.prob_margin(m, 0)
    assert abs(hw + aw + dr - 1.0) < 1e-9


def test_scoreline_matrix_matches_1x2(model_cfg, match_factory):
    """The matrix collapsed to 1X2 must equal the model's own predict() 1X2."""
    model = DixonColesInplayModel(model_cfg)
    match = match_factory(minute=30, home_score=1, away_score=0, home_xg=1.2, away_xg=0.4)
    m = model.scoreline_matrix(match)
    assert abs(m.sum() - 1.0) < 1e-9
    ph, pd, pa = _collapse_1x2(m)  # matrix holds FINAL scores
    p = model.predict(match)
    assert abs(ph - p.p_home) < 1e-6 and abs(pd - p.p_draw) < 1e-6 and abs(pa - p.p_away) < 1e-6


def test_scoreline_matrix_finished_is_degenerate(model_cfg, match_factory):
    from wc_kalshi.models.schemas import MatchPeriod
    model = DixonColesInplayModel(model_cfg)
    match = match_factory(minute=90, period=MatchPeriod.FULL_TIME, home_score=2, away_score=1,
                          status="finished")
    m = model.scoreline_matrix(match)
    assert m[2, 1] == 1.0 and abs(m.sum() - 1.0) < 1e-9
