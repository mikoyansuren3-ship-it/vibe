"""Knockout-stage math (modeling/knockout.py + inplay.scoreline_matrix_et, plan P2.1):
advance composition, method-of-victory decomposition, go-to-ET/penalties, and a Monte-Carlo
cross-check that the closed form matches the nested reg → ET → shootout process."""

import numpy as np

from wc_kalshi.config import load_config
from wc_kalshi.modeling.inplay import DixonColesInplayModel
from wc_kalshi.modeling.knockout import ET_TOP_N, advance_probs, knockout_breakdown
from wc_kalshi.models.schemas import MatchContext, MatchPeriod, MatchSnapshot, TeamStats


def _model():
    return DixonColesInplayModel(load_config(load_env=False, use_local=False).model)


def _pre(home_elo=1950.0, away_elo=1750.0, *, minute=0, hs=0, as_=0, period=MatchPeriod.PRE,
         home_red=0, away_red=0, hxg=None, axg=None):
    return MatchSnapshot(
        match_id="ko", provider="x", home_team="H", away_team="A", minute=minute, period=period,
        home_score=hs, away_score=as_, status="scheduled" if period is MatchPeriod.PRE else "live",
        home=TeamStats(xg=hxg, red_cards=home_red), away=TeamStats(xg=axg, red_cards=away_red),
        context=MatchContext(neutral_venue=True, home_elo=home_elo, away_elo=away_elo),
    )


def test_et_matrix_is_valid():
    m = _model()
    M_et = m.scoreline_matrix_et(_pre())
    assert M_et.shape == (9, 9)  # _ET_MAX_GOALS=8 -> 9x9
    assert abs(M_et.sum() - 1.0) < 1e-9
    assert (M_et >= 0).all()


def test_advance_sums_to_one():
    m = _model()
    for he, ae in [(1950, 1750), (1700, 2050), (1800, 1800)]:
        adv_h, adv_a = advance_probs(m, _pre(he, ae))
        assert abs(adv_h + adv_a - 1.0) < 1e-9


def test_method_of_victory_decomposes_into_advance():
    b = knockout_breakdown(_model(), _pre(1950, 1750))
    for side in (0, 1):  # home, away
        moV = b["win_regulation"][side] + b["win_extra_time"][side] + b["win_penalties"][side]
        assert abs(moV - b["advance"][side]) < 1e-12
    # All six method cells + nothing else sum to 1.
    total = sum(b[k][0] + b[k][1] for k in ("win_regulation", "win_extra_time", "win_penalties"))
    assert abs(total - 1.0) < 1e-9


def test_go_to_et_and_penalties_identities():
    m = _model()
    match = _pre(1850, 1800)
    b = knockout_breakdown(m, match)
    M_reg = m.scoreline_matrix(match)
    p_tie_reg = float(np.trace(M_reg))  # diagonal = P(draw in regulation)
    assert abs(b["go_to_extra_time"] - p_tie_reg) < 1e-9
    # go_to_penalties = P(tie reg) * P(tie ET) <= go_to_extra_time
    assert 0.0 < b["go_to_penalties"] <= b["go_to_extra_time"]


def test_stronger_team_more_likely_to_advance():
    from wc_kalshi.modeling.knockout import pens_home_win

    m = _model()
    adv_h, adv_a = advance_probs(m, _pre(home_elo=2050, away_elo=1650))
    assert adv_h > adv_a and adv_h > 0.6  # a clear favourite advances more often
    # The shootout itself is a neutral coin flip (no Elo tilt), regardless of strength.
    assert pens_home_win(_pre(home_elo=2050, away_elo=1650)) == 0.5
    # Equal Elo sits modestly above 0.5 for the nominal "home" side — the model's base-rate
    # attacking asymmetry (base_home_xg > base_away_xg) applies even at a neutral venue; it is
    # a pre-existing whole-model property, not a knockout effect.
    even_h, _ = advance_probs(m, _pre(home_elo=1800, away_elo=1800))
    assert 0.5 < even_h < 0.62


def test_et_scorelines_top_n_descending():
    b = knockout_breakdown(_model(), _pre())
    scores = b["et_scorelines"]
    assert len(scores) == ET_TOP_N
    probs = [p for _, p in scores]
    assert probs == sorted(probs, reverse=True)


def test_advance_matches_monte_carlo():
    m = _model()
    match = _pre(home_elo=1980, away_elo=1740)
    adv_home, _ = advance_probs(m, match)
    M_reg, M_et = m.scoreline_matrix(match), m.scoreline_matrix_et(match)
    rng = np.random.default_rng(0)

    def sample(M, n):
        flat = (M / M.sum()).ravel()
        idx = rng.choice(flat.size, size=n, p=flat)
        return idx // M.shape[1], idx % M.shape[1]

    n = 120_000
    ri, rj = sample(M_reg, n)
    home_wins = ri > rj
    reg_draw = ri == rj
    nd = int(reg_draw.sum())
    ei, ej = sample(M_et, nd)
    et_home, et_draw = ei > ej, ei == ej
    resolved_home = et_home.copy()
    resolved_home[et_draw] = rng.random(int(et_draw.sum())) < 0.5  # shootout coin flip
    mc = (int(home_wins.sum()) + int(resolved_home.sum())) / n
    assert abs(mc - adv_home) < 0.01  # closed form matches the nested simulation
