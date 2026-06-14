"""Dixon-Coles in-play model behaviour."""

from wc_kalshi.modeling.inplay import DixonColesInplayModel
from wc_kalshi.models.schemas import MatchPeriod, Outcome


def _model(model_cfg):
    return DixonColesInplayModel(model_cfg)


def test_probabilities_sum_to_one(model_cfg, match_factory):
    m = match_factory(minute=30, home_score=1, away_score=0, home_xg=1.1, away_xg=0.4)
    p = _model(model_cfg).predict(m)
    assert abs(p.p_home + p.p_draw + p.p_away - 1.0) < 1e-9


def test_finished_match_is_degenerate(model_cfg, match_factory):
    m = match_factory(minute=90, period=MatchPeriod.FULL_TIME, home_score=2, away_score=1, status="finished")
    p = _model(model_cfg).predict(m)
    assert p.p_home == 1.0 and p.p_draw == 0.0 and p.p_away == 0.0


def test_late_lead_is_strong(model_cfg, match_factory):
    m = match_factory(minute=88, home_score=1, away_score=0, period=MatchPeriod.SECOND_HALF)
    p = _model(model_cfg).predict(m)
    assert p.p_home > 0.85


def test_red_card_against_away_helps_home(model_cfg, match_factory):
    base = match_factory(minute=40, home_score=0, away_score=0, home_xg=0.4, away_xg=0.4)
    red = match_factory(minute=40, home_score=0, away_score=0, home_xg=0.4, away_xg=0.4, away_red=1)
    p_base = _model(model_cfg).predict(base)
    p_red = _model(model_cfg).predict(red)
    assert p_red.p_home > p_base.p_home


def test_live_xg_dominance_shifts_probability(model_cfg, match_factory):
    low = match_factory(minute=60, home_xg=0.3, away_xg=0.3)
    high = match_factory(minute=60, home_xg=2.2, away_xg=0.3)
    p_low = _model(model_cfg).predict(low)
    p_high = _model(model_cfg).predict(high)
    assert p_high.p_home > p_low.p_home


def test_stronger_elo_favoured_pre_match(model_cfg, match_factory):
    m = match_factory(minute=0, home_elo=2050, away_elo=1700)
    p = _model(model_cfg).predict(m)
    assert p.get(Outcome.HOME) > p.get(Outcome.AWAY)
