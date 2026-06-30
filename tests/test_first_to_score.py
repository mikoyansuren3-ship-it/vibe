"""First-to-score head (modeling.first_to_score, plan P3): a competing-Poisson first-passage
split off the SAME backbone remaining rates that build ``M`` — so it stays coherent — that
collapses to a degenerate result once the first goal is in, and refuses to price a game whose
first scorer can't be recovered."""

import math

import pytest

from wc_kalshi.config import load_config
from wc_kalshi.modeling.first_to_score import (
    FirstToScore,
    first_scorer_from_ticks,
    first_to_score,
    first_to_score_rates,
)
from wc_kalshi.modeling.inplay import DixonColesInplayModel
from wc_kalshi.modeling.poisson import remaining_goal_matrix
from wc_kalshi.models.schemas import MatchContext, MatchPeriod, MatchSnapshot


def _model():
    return DixonColesInplayModel(load_config(load_env=False, use_local=False).model)


def _snap(hs=0, as_=0, minute=0, period=MatchPeriod.PRE, status="scheduled",
          home_elo=1900.0, away_elo=1800.0):
    return MatchSnapshot(
        match_id="f", provider="x", home_team="H", away_team="A",
        minute=minute, period=period, status=status, home_score=hs, away_score=as_,
        context=MatchContext(neutral_venue=True, home_elo=home_elo, away_elo=away_elo),
    )


# -- the closed-form rate split ------------------------------------------------ #
@pytest.mark.parametrize("lam,mu", [(1.4, 1.1), (0.3, 2.0), (0.05, 0.05), (3.0, 0.2)])
def test_rates_sum_to_one(lam, mu):
    p = first_to_score_rates(lam, mu)
    assert abs(sum(p) - 1.0) < 1e-12
    assert all(x >= 0.0 for x in p)


def test_rate_split_is_proportional_to_lambda():
    # P(home first) / P(away first) == λ / μ (the ordering is rate-proportional).
    p_home, p_away, _ = first_to_score_rates(1.8, 0.6)
    assert abs(p_home / p_away - 1.8 / 0.6) < 1e-12


def test_symmetry_when_rates_equal():
    p_home, p_away, _ = first_to_score_rates(1.3, 1.3)
    assert abs(p_home - p_away) < 1e-12


def test_no_goal_collapses_to_one_when_rates_vanish():
    assert first_to_score_rates(0.0, 0.0) == (0.0, 0.0, 1.0)
    # And degrades smoothly: tiny rates ⇒ almost-certain no-goal.
    _, _, png = first_to_score_rates(1e-6, 1e-6)
    assert png > 0.99999


def test_high_rates_make_a_goal_near_certain():
    _, _, png = first_to_score_rates(4.0, 3.0)
    assert png < 0.001


def test_home_first_monotone_in_lambda():
    base = first_to_score_rates(1.0, 1.0)[0]
    more = first_to_score_rates(1.5, 1.0)[0]
    assert more > base


def test_no_goal_equals_backbone_matrix_corner():
    # Coherence with M: P(no goal) is exactly e^(−Λ) = e^(−λ)·e^(−μ), the remaining-goal
    # matrix's [0,0] cell at ρ=0 — agreeing up to the matrix's truncation renormalization
    # (remaining_goal_matrix divides by the truncated total, inflating [0,0] by the tail).
    lam, mu = 1.7, 0.9
    _, _, png = first_to_score_rates(lam, mu)
    assert abs(png - math.exp(-(lam + mu))) < 1e-12  # the exact identity
    m = remaining_goal_matrix(lam, mu, rho=0.0, max_goals=10)
    assert abs(png - float(m[0, 0])) < 1e-6  # coherent to truncation


# -- the live head, coherent with the model ------------------------------------ #
def test_live_zero_zero_is_unsettled_and_normalized():
    model = _model()
    snap = _snap(period=MatchPeriod.FIRST_HALF, status="live", minute=20)
    fts = first_to_score(model, snap)
    assert not fts.settled and not fts.ambiguous and fts.tradeable
    assert abs(fts.p_home + fts.p_away + fts.p_no_goal - 1.0) < 1e-12
    # Favourite (higher Elo home) is likelier to open the scoring than the underdog.
    assert fts.p_home > fts.p_away


def test_head_no_goal_matches_remaining_rate_split():
    # The head consumes the very rates it shares with the scoreline matrix.
    model = _model()
    snap = _snap(period=MatchPeriod.FIRST_HALF, status="live", minute=15)
    lam, mu = model.remaining_rates(snap)
    assert first_to_score(model, snap).p_no_goal == pytest.approx(math.exp(-(lam + mu)))


def test_one_sided_score_settles_to_that_team():
    model = _model()
    home_lead = first_to_score(model, _snap(hs=1, as_=0, period=MatchPeriod.SECOND_HALF, status="live"))
    assert home_lead == FirstToScore(1.0, 0.0, 0.0, settled=True)
    away_lead = first_to_score(model, _snap(hs=0, as_=2, period=MatchPeriod.SECOND_HALF, status="live"))
    assert away_lead == FirstToScore(0.0, 1.0, 0.0, settled=True)


def test_both_scored_without_history_is_ambiguous():
    model = _model()
    fts = first_to_score(model, _snap(hs=1, as_=1, period=MatchPeriod.SECOND_HALF, status="live"))
    assert fts.settled and fts.ambiguous and not fts.tradeable
    assert fts.p_home is None and fts.p_away is None and fts.p_no_goal is None


def test_both_scored_with_history_resolves_first_scorer():
    model = _model()
    history = [
        _snap(0, 0, minute=5, period=MatchPeriod.FIRST_HALF, status="live"),
        _snap(0, 1, minute=30, period=MatchPeriod.FIRST_HALF, status="live"),  # away opened
        _snap(1, 1, minute=70, period=MatchPeriod.SECOND_HALF, status="live"),
    ]
    fts = first_to_score(model, history[-1], history=history)
    assert fts == FirstToScore(0.0, 1.0, 0.0, settled=True)


def test_finished_goalless_settles_to_no_goal():
    model = _model()
    fts = first_to_score(model, _snap(0, 0, minute=90, period=MatchPeriod.FULL_TIME, status="finished"))
    assert fts == FirstToScore(0.0, 0.0, 1.0, settled=True)


# -- tick-stream first-scorer recovery ----------------------------------------- #
def test_first_scorer_from_ticks_home():
    history = [_snap(0, 0), _snap(0, 0), _snap(1, 0), _snap(1, 1)]
    assert first_scorer_from_ticks(history) == "home"


def test_first_scorer_from_ticks_away():
    assert first_scorer_from_ticks([_snap(0, 0), _snap(0, 1)]) == "away"


def test_first_scorer_ambiguous_when_both_appear_in_one_gap():
    # 0-0 jumps straight to 1-1 between captures — order is lost.
    assert first_scorer_from_ticks([_snap(0, 0), _snap(1, 1)]) is None


def test_first_scorer_none_when_goalless():
    assert first_scorer_from_ticks([_snap(0, 0), _snap(0, 0)]) is None
    assert first_scorer_from_ticks(None) is None
    assert first_scorer_from_ticks([]) is None


# -- export wiring: the KXWCFTTS board group ----------------------------------- #
def _cfg():
    return load_config(load_env=False, use_local=False)


def _fts_groups(bundle):
    return [g for g in (bundle.get("all_markets") or []) if g["series"] in ("KXWCFTTS", "KXWCTTSF")]


def test_upcoming_bundle_prices_first_to_score():
    from wc_kalshi.backtest.export import build_upcoming_bundle

    bundle = build_upcoming_bundle(_cfg(), _snap())  # pre-kickoff, 0-0
    groups = _fts_groups(bundle)
    assert len(groups) == 1 and groups[0]["priceable"]
    contracts = groups[0]["contracts"]
    assert [c["label"] for c in contracts] == ["H scores first", "A scores first", "No goal"]
    assert abs(sum(c["model"] for c in contracts) - 1.0) < 1e-9
    assert contracts[0]["model"] > contracts[1]["model"]  # Elo favourite opens scoring more often


def test_live_bundle_ambiguous_keeps_market_only_quote():
    from wc_kalshi.backtest.export import build_live_bundle

    history = [_snap(0, 0, 5, MatchPeriod.FIRST_HALF, "live"),
               _snap(1, 1, 80, MatchPeriod.SECOND_HALF, "live")]  # 0-0 → 1-1 in one gap
    quotes = [("KXWCFTTS", "t", "H", None, 30, 34)]
    bundle = build_live_bundle(_cfg(), "f", history, [], all_quote_rows=quotes)
    groups = _fts_groups(bundle)
    # Refuses to price (ambiguous) → the captured contract survives as market-only, not modelled.
    assert len(groups) == 1 and not groups[0]["priceable"]


def test_live_bundle_resolved_first_scorer_drops_projection():
    from wc_kalshi.backtest.export import build_live_bundle

    history = [_snap(0, 0, 5, MatchPeriod.FIRST_HALF, "live"),
               _snap(0, 1, 30, MatchPeriod.FIRST_HALF, "live"),
               _snap(1, 1, 80, MatchPeriod.SECOND_HALF, "live")]
    bundle = build_live_bundle(_cfg(), "f", history, [], all_quote_rows=[("KXWCTOTAL", "t", "Over 2.5", 2.5, 40, 44)])
    assert _fts_groups(bundle) == []  # settled market is no projection


def test_live_bundle_no_quotes_synthesizes_no_box():
    from wc_kalshi.backtest.export import build_live_bundle

    history = [_snap(0, 0, 75, MatchPeriod.SECOND_HALF, "live")]
    bundle = build_live_bundle(_cfg(), "f", history, [], all_quote_rows=None)
    assert _fts_groups(bundle) == []  # live board stays an enrichment of real markets
