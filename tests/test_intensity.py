"""Intensity-engine levers (modeling/intensity.py, plan P0.1).

Each lever must be a no-op at its default (so the backbone refactor changed no behaviour)
and shift rates in the documented direction when turned on.
"""

from wc_kalshi.config import load_config
from wc_kalshi.modeling.inplay import DixonColesInplayModel
from wc_kalshi.modeling.intensity import (
    credibility_weight,
    red_card_factors,
    remaining_fraction,
    score_state_mults,
)
from wc_kalshi.models.schemas import MatchContext, MatchPeriod, MatchSnapshot, TeamStats


def test_remaining_fraction_flat_is_legacy():
    # slope=0 reproduces the flat (1-u) profile, so f_rem*90 == minutes remaining.
    for minute in (0, 20, 45, 70, 90):
        assert abs(remaining_fraction(minute, 0.0) - (1 - minute / 90)) < 1e-12
    # Endpoints are pinned regardless of slope.
    assert remaining_fraction(0, 0.4) == 1.0
    assert remaining_fraction(90, 0.4) == 0.0
    # slope>0 => more of the match's goals are still to come at half-time.
    assert remaining_fraction(45, 0.4) > remaining_fraction(45, 0.0)


def test_score_state_mults_flat_then_graded():
    # per_goal=0 ignores margin size (legacy single multiplier).
    assert score_state_mults(1, leader_mult=0.92, chaser_mult=1.1, per_goal=0.0) == (0.92, 1.1)
    assert score_state_mults(3, leader_mult=0.92, chaser_mult=1.1, per_goal=0.0) == (0.92, 1.1)
    assert score_state_mults(-1, leader_mult=0.92, chaser_mult=1.1, per_goal=0.0) == (1.1, 0.92)
    assert score_state_mults(0, leader_mult=0.92, chaser_mult=1.1, per_goal=0.5) == (1.0, 1.0)
    # graded: a 2-goal lead suppresses the leader more and pushes the chaser harder.
    lead2, chase2 = score_state_mults(2, leader_mult=0.92, chaser_mult=1.1, per_goal=0.5)
    assert lead2 < 0.92 and chase2 > 1.1


def test_red_card_factors_legacy_and_asymmetric():
    assert red_card_factors(0.45, None) == (0.45, 1.0 + (1.0 - 0.45))  # symmetric legacy
    assert red_card_factors(0.45, 1.10) == (0.45, 1.10)  # asymmetric override


def test_credibility_weight_saturates():
    assert credibility_weight(0.0, 1.3) == 0.0  # no info -> pure prior
    assert credibility_weight(-1.0, 1.3) == 0.0
    w_low, w_high = credibility_weight(0.2, 1.3), credibility_weight(2.5, 1.3)
    assert 0.0 < w_low < w_high < 1.0  # monotone increasing, never reaches 1


def _late_quiet_match():
    """Minute 80, 0-0, tiny xG — the case where the flat elapsed/90 weight over-trusts a
    low live rate and over-rates the draw."""
    return MatchSnapshot(
        match_id="t", provider="x", home_team="H", away_team="A", minute=80,
        period=MatchPeriod.SECOND_HALF, home_score=0, away_score=0,
        home=TeamStats(xg=0.1), away=TeamStats(xg=0.1),
        context=MatchContext(home_elo=1850, away_elo=1800),
    )


def test_level_game_rates_are_gamestate_neutral():
    cfg = load_config(load_env=False, use_local=False)
    m = DixonColesInplayModel(cfg.model)
    # Home leading by 2 at minute 60 — the game-state multiplier would suppress the leader.
    s = MatchSnapshot(
        match_id="t", provider="x", home_team="H", away_team="A", minute=60,
        period=MatchPeriod.SECOND_HALF, home_score=2, away_score=0,
        home=TeamStats(xg=1.5), away=TeamStats(xg=0.3),
        context=MatchContext(home_elo=1950, away_elo=1700),
    )
    # The exposed level-game rates equal the raw blend (score treated as level), and are
    # positive — the conditional rates a future ET / first-to-score head consumes.
    assert m.level_game_per_minute_rates(s) == m._blended_per_minute_rates(s, 60)
    lh, la = m.level_game_per_minute_rates(s)
    assert lh > 0 and la > 0


def test_credibility_mode_downweights_low_info_late_game():
    cfg = load_config(load_env=False, use_local=False)
    linear = DixonColesInplayModel(cfg.model)
    credibility = DixonColesInplayModel(cfg.model.model_copy(update={"xg_blend_mode": "credibility"}))
    m = _late_quiet_match()
    p_lin = linear.predict(m)
    p_cred = credibility.predict(m)
    # Credibility keeps the Elo prior (more remaining goals) instead of trusting 0.1 xG,
    # so it assigns LESS probability to the draw than the legacy flat-weight blend.
    assert p_cred.p_draw < p_lin.p_draw
