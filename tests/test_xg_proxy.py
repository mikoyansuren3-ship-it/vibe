"""Shot-based xG proxy + the model's missing-xG fallback.

Regression cover for the fix where API-Football's in-play WC feed supplies no
``expected_goals``: missing xG must NOT be read as 0.0 (which suppressed the
remaining-goal rate and over-rated the draw). Instead: real xG > proxy > prior.
"""

from wc_kalshi.modeling.inplay import DixonColesInplayModel
from wc_kalshi.modeling.xg_proxy import (
    DEFAULT_W_OFF,
    DEFAULT_W_SOT,
    observed_xg,
    proxy_xg,
)
from wc_kalshi.models.schemas import (
    MatchContext,
    MatchPeriod,
    MatchSnapshot,
    TeamStats,
)


def _match(home: TeamStats, away: TeamStats, *, minute=80, hs=1, as_=1):
    return MatchSnapshot(
        match_id="m",
        provider="test",
        home_team="Home",
        away_team="Away",
        minute=minute,
        period=MatchPeriod.SECOND_HALF,
        home_score=hs,
        away_score=as_,
        home=home,
        away=away,
        status="live",
        context=MatchContext(neutral_venue=True, home_elo=1800.0, away_elo=1800.0),
    )


# --- proxy_xg -------------------------------------------------------------- #
def test_proxy_xg_uses_shot_weights():
    stats = TeamStats(shots=7, shots_on_target=3)  # 3 SOT, 4 off-target
    expected = DEFAULT_W_SOT * 3 + DEFAULT_W_OFF * 4
    assert abs(proxy_xg(stats) - expected) < 1e-9


def test_proxy_xg_none_without_shot_signal():
    # No shots/SOT/big-chances at all is indistinguishable from "provider doesn't
    # track shots" -> None, so the caller falls back to the prior, not to zero.
    assert proxy_xg(TeamStats()) is None


# --- observed_xg precedence ------------------------------------------------ #
def test_observed_prefers_real_positive_xg():
    # A real, informative feed value wins (the path a true live-xG provider takes).
    stats = TeamStats(xg=1.5, shots=10, shots_on_target=5)
    assert observed_xg(stats) == 1.5


def test_observed_falls_back_to_proxy_when_xg_missing():
    stats = TeamStats(xg=None, shots=8, shots_on_target=3)
    assert abs(observed_xg(stats) - proxy_xg(stats)) < 1e-9


def test_observed_treats_legacy_zero_with_shots_as_missing():
    # Captured-before-the-fix snapshots stored missing xG as 0.0; with shots present
    # that 0.0 is an artifact, so the proxy should still kick in (helps DB replay).
    stats = TeamStats(xg=0.0, shots=8, shots_on_target=3)
    assert observed_xg(stats) == proxy_xg(stats) > 0.0


def test_observed_passes_through_real_zero_without_shots():
    assert observed_xg(TeamStats(xg=0.0)) == 0.0
    assert observed_xg(TeamStats(xg=None)) is None


# --- model behaviour ------------------------------------------------------- #
def test_missing_xg_does_not_collapse_to_draw(model_cfg):
    """At 1-1, 80', a blind match (no xG, no shots) must keep more late-winner
    probability than a real-zero-xG match — i.e. missing != suppressed."""
    model = DixonColesInplayModel(model_cfg)
    blind = _match(TeamStats(xg=None), TeamStats(xg=None))  # truly no signal -> prior
    real_zero = _match(TeamStats(xg=0.0), TeamStats(xg=0.0))  # provider says 0 chances
    p_blind = model.predict(blind)
    p_zero = model.predict(real_zero)
    # Real-zero suppresses the remaining rate -> higher draw; blind falls back to the
    # prior -> lower draw (more room for a late winner).
    assert p_blind.p_draw < p_zero.p_draw


def test_proxy_feeds_observed_xg_into_model(model_cfg):
    model = DixonColesInplayModel(model_cfg)
    # Japan-vs-Sweden-like live state: both sides shooting, feed gives no xG -> the
    # model reads the shot-derived proxy rather than 0.0.
    jpn = TeamStats(xg=None, shots=7, shots_on_target=3)
    swe = TeamStats(xg=None, shots=8, shots_on_target=3)
    assert abs(model._observed_xg(jpn) - (DEFAULT_W_SOT * 3 + DEFAULT_W_OFF * 4)) < 1e-9
    assert abs(model._observed_xg(swe) - (DEFAULT_W_SOT * 3 + DEFAULT_W_OFF * 5)) < 1e-9


def test_heavy_shooting_lowers_draw_vs_blind(model_cfg):
    """Two teams generating lots of chances (proxy xG above the prior rate) leave
    more room for a late winner -> a lower draw than a blind 1-1 (prior fallback)."""
    model = DixonColesInplayModel(model_cfg)
    heavy = TeamStats(xg=None, shots=15, shots_on_target=7)  # proxy ~1.8 each
    p_heavy = model.predict(_match(heavy, TeamStats(xg=None, shots=15, shots_on_target=7)))
    p_blind = model.predict(_match(TeamStats(xg=None), TeamStats(xg=None)))
    assert p_heavy.p_draw < p_blind.p_draw
