"""Phase-3 vectorization equivalence: the numpy collapse ops must match the original
pure-Python O(G²) loops to within the golden 1e-12 tolerance, on random matrices of many
shapes. The naive references below are copies of the pre-vectorization implementations."""

import numpy as np

from wc_kalshi.modeling.derived import supremacy_pmf, total_goals_pmf
from wc_kalshi.modeling.knockout import _win_probs
from wc_kalshi.modeling.poisson import outcome_probs

TOL = 1e-12


def _ref_outcome_probs(matrix, current_diff=0):
    n = matrix.shape[0]
    ph = pd = pa = 0.0
    for x in range(n):
        for y in range(n):
            final = current_diff + x - y
            if final > 0:
                ph += matrix[x, y]
            elif final == 0:
                pd += matrix[x, y]
            else:
                pa += matrix[x, y]
    return ph, pd, pa


def _ref_total_goals_pmf(m):
    n_h, n_a = m.shape
    pmf = np.zeros(n_h + n_a - 1)
    for i in range(n_h):
        for j in range(n_a):
            pmf[i + j] += m[i, j]
    return pmf


def _ref_supremacy_pmf(m):
    n_h, n_a = m.shape
    out: dict[int, float] = {}
    for i in range(n_h):
        for j in range(n_a):
            out[i - j] = out.get(i - j, 0.0) + float(m[i, j])
    return out


def _ref_win_probs(m):
    n_h, n_a = m.shape
    home = draw = away = 0.0
    for i in range(n_h):
        for j in range(n_a):
            v = float(m[i, j])
            if i > j:
                home += v
            elif i == j:
                draw += v
            else:
                away += v
    return home, draw, away


def _prob_matrix(rng, n_h, n_a):
    m = rng.random((n_h, n_a))
    return m / m.sum()  # a joint probability matrix (the real callers always pass one)


def test_outcome_probs_matches_naive_reference():
    rng = np.random.default_rng(0)
    for _ in range(200):
        n = int(rng.integers(1, 13))
        m = _prob_matrix(rng, n, n)  # square: outcome_probs iterates shape[0] on both axes
        cd = int(rng.integers(-4, 5))
        got, want = outcome_probs(m, cd), _ref_outcome_probs(m, cd)
        assert all(abs(g - w) < TOL for g, w in zip(got, want)), (n, cd, got, want)


def test_total_goals_pmf_matches_naive_reference():
    rng = np.random.default_rng(1)
    for _ in range(200):
        m = _prob_matrix(rng, int(rng.integers(1, 13)), int(rng.integers(1, 13)))
        got, want = total_goals_pmf(m), _ref_total_goals_pmf(m)
        assert got.shape == want.shape
        assert np.max(np.abs(got - want)) < TOL


def test_supremacy_pmf_matches_naive_reference():
    rng = np.random.default_rng(2)
    for _ in range(200):
        m = _prob_matrix(rng, int(rng.integers(1, 13)), int(rng.integers(1, 13)))
        got, want = supremacy_pmf(m), _ref_supremacy_pmf(m)
        assert set(got) == set(want)  # identical margin keys
        assert all(abs(got[d] - want[d]) < TOL for d in want)


def test_win_probs_matches_naive_reference():
    rng = np.random.default_rng(3)
    for _ in range(200):
        m = _prob_matrix(rng, int(rng.integers(1, 13)), int(rng.integers(1, 13)))
        got, want = _win_probs(m), _ref_win_probs(m)
        assert all(abs(g - w) < TOL for g, w in zip(got, want))
