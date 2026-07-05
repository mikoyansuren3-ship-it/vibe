"""Fitting model constants from data (no more hand-picked magic numbers)."""

from wc_kalshi.ingestion.football.simulated import FIXTURES, simulate_full_match
from wc_kalshi.modeling.fit import FIT_CHECKPOINTS, fit_constants


def test_fit_returns_constants_and_does_not_worsen(cfg):
    matches = [
        simulate_full_match(seed=s, fixture=FIXTURES[s % len(FIXTURES)], match_id=f"fit-{s}")
        for s in range(20)
    ]
    res = fit_constants(matches, cfg.model, passes=1)
    # all five fittable constants are returned
    assert set(res.params) == {
        "live_xg_weight", "red_card_xg_penalty", "elo_tilt", "leader_mult", "chaser_mult"
    }
    assert res.n_samples == len(matches) * len(FIT_CHECKPOINTS)
    # coordinate descent never increases log-loss versus the starting config
    assert res.logloss_after <= res.logloss_before + 1e-9


def test_fitted_values_are_in_grid(cfg):
    matches = [simulate_full_match(seed=s, match_id=f"f-{s}") for s in range(10)]
    res = fit_constants(matches, cfg.model, passes=1)
    assert 0.0 < res.params["live_xg_weight"] <= 1.0
    assert 0.0 < res.params["red_card_xg_penalty"] < 1.0


def test_checkpoint_snaps_takes_a_sparse_snapshot_once():
    """A snapshot that clears several checkpoints at once (sparse capture) is taken ONCE, not
    once per checkpoint — otherwise the fit over-weights that lone snapshot."""
    from types import SimpleNamespace

    from wc_kalshi.modeling.fit import FIT_CHECKPOINTS, _checkpoint_snaps
    from wc_kalshi.models.schemas import MatchPeriod

    def snap(minute, *, live=True):
        return SimpleNamespace(
            minute=minute, period=MatchPeriod.SECOND_HALF if live else MatchPeriod.PRE
        )

    # One live snapshot at minute 45 clears checkpoints 10, 25 and 40 simultaneously.
    out = _checkpoint_snaps([snap(3, live=False), snap(45)])
    assert len(out) == 1 and out[0].minute == 45  # taken once, not three times

    # Dense capture — one snapshot per checkpoint minute — is unchanged: one sample each.
    assert len(_checkpoint_snaps([snap(cp) for cp in FIT_CHECKPOINTS])) == len(FIT_CHECKPOINTS)
