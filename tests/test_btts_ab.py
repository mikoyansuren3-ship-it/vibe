"""Correctness pins for the BTTS A/B harness (scripts/btts_ab.py).

The harness's KEEP-OFF verdict rests on the Frank copula being a correct negative-dependence
model. These pin that math (independence at theta=0, Poisson marginals preserved, theta sign sets
the covariance / BTTS direction) and the rho-persist arm's defining behaviour, so a future edit
can't silently corrupt the study. Pure functions only; no fitting/bootstrap."""

import pathlib
import sys

import numpy as np
import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
import btts_ab as ba  # noqa: E402

from wc_kalshi.config import load_config
from wc_kalshi.modeling.poisson import poisson_pmf
from wc_kalshi.models.schemas import MatchContext, MatchPeriod, MatchSnapshot


def _cov(M):
    i, j = np.arange(M.shape[0]), np.arange(M.shape[1])
    ei = (i[:, None] * M).sum()
    ej = (j[None, :] * M).sum()
    return (np.outer(i, j) * M).sum() - ei * ej


def _btts(M):
    return float(M[1:, 1:].sum())


def test_theta_zero_is_independent_poisson():
    M = ba.frank_remaining_matrix(1.5, 1.1, 0.0, 12)
    ph = np.array([poisson_pmf(1.5, k) for k in range(13)])
    pa = np.array([poisson_pmf(1.1, k) for k in range(13)])
    indep = np.outer(ph, pa)
    indep /= indep.sum()
    assert np.allclose(M, indep, atol=1e-9)


@pytest.mark.parametrize("lam,mu", [(1.5, 1.1), (0.7, 2.3), (2.0, 2.0)])
def test_marginals_recover_poisson(lam, mu):
    M = ba.frank_remaining_matrix(lam, mu, -1.5, 12)
    ph = np.array([poisson_pmf(lam, k) for k in range(13)])
    ph /= ph.sum()
    pa = np.array([poisson_pmf(mu, k) for k in range(13)])
    pa /= pa.sum()
    assert np.allclose(M.sum(axis=1), ph, atol=2e-9)  # home marginal
    assert np.allclose(M.sum(axis=0), pa, atol=2e-9)  # away marginal


def test_theta_sign_sets_dependence_direction():
    M0 = ba.frank_remaining_matrix(1.5, 1.1, 0.0, 12)
    Mneg = ba.frank_remaining_matrix(1.5, 1.1, -1.5, 12)
    Mpos = ba.frank_remaining_matrix(1.5, 1.1, +1.5, 12)
    assert _cov(Mneg) < 0 < _cov(Mpos)          # theta<0 => negative covariance
    assert abs(_cov(M0)) < 1e-9                  # independence has zero covariance
    assert _btts(Mneg) < _btts(M0) < _btts(Mpos)  # negative dependence LOWERS BTTS


def test_copula_matrix_is_a_normalized_pmf():
    M = ba.frank_remaining_matrix(1.4, 1.2, -1.0, 12)
    assert (M >= 0).all()
    assert M.sum() == pytest.approx(1.0)


def _snap(hs, as_, minute):
    return MatchSnapshot(match_id="b", provider="x", home_team="H", away_team="A", minute=minute,
                         period=MatchPeriod.SECOND_HALF, status="live", home_score=hs, away_score=as_,
                         context=MatchContext(neutral_venue=True, home_elo=1850, away_elo=1800))


def test_rho_persist_keeps_rho_active_after_a_goal():
    cfg = load_config(load_env=False, use_local=False).model
    prod = ba.DixonColesInplayModel(cfg)
    persist = ba.RhoPersistModel(cfg)
    after_goal = _snap(1, 0, 60)
    # Production zeroes the DC correction once any goal is in; rho_persist keeps it (faded by time).
    assert prod._effective_rho(after_goal) == 0.0
    assert persist._effective_rho(after_goal) == pytest.approx(cfg.draw_rho * (90 - 60) / 90)
    # Both agree at 0-0 (the only state production applies it).
    level = _snap(0, 0, 60)
    assert persist._effective_rho(level) == pytest.approx(prod._effective_rho(level))


def test_frank_model_matrix_shifts_onto_current_score():
    cfg = load_config(load_env=False, use_local=False).model
    fm = ba.FrankModel(cfg)
    fm.theta = -1.0
    M = fm.scoreline_matrix(_snap(1, 0, 30))
    assert M.sum() == pytest.approx(1.0)
    # No mass below the current score (home already has 1): rows < 1 are empty.
    assert M[0, :].sum() == pytest.approx(0.0)
