"""Backbone correctness invariants (modeling program Phase 0.2).

These pin properties the whole per-market stack relies on, so a refactor of the
intensity engine / matrix math can't silently break them:

  * the Dixon-Coles ``draw_rho`` SIGN convention — a negative rho must RAISE P(draw)
    (it feeds P(level) → advance / method-of-victory / goes-to-ET and the 0-0 corner
    of BTTS / first-to-score);
  * the supremacy/margin collapse — ``prob_margin`` over the joint matrix must equal
    the Skellam (difference-of-Poissons) distribution at rho=0, checked against an
    INDEPENDENT 1-D convolution oracle (not the same double-loop).
"""

import numpy as np

from wc_kalshi.modeling import derived
from wc_kalshi.modeling.poisson import outcome_probs, poisson_pmf, remaining_goal_matrix

_CONFIG_DRAW_RHO = -0.05  # config/default.yaml model.draw_rho


def _p_draw(rho: float, lam: float = 1.3, mu: float = 1.3) -> float:
    m = remaining_goal_matrix(lam, mu, rho=rho, max_goals=10)
    return outcome_probs(m, 0)[1]


def test_negative_draw_rho_raises_p_draw():
    """The repo's default draw_rho=-0.05 must INCREASE the draw probability vs rho=0;
    P(draw) is monotone decreasing in rho. Guards the sign convention of dc_tau."""
    p_neg = _p_draw(-0.05)
    p_zero = _p_draw(0.0)
    p_pos = _p_draw(+0.05)
    assert p_neg > p_zero > p_pos, (p_neg, p_zero, p_pos)
    # The configured default is on the draw-raising side of neutral.
    assert _p_draw(_CONFIG_DRAW_RHO) > p_zero
    # Holds for asymmetric (favourite) rates too.
    assert _p_draw(-0.05, 2.1, 0.8) > _p_draw(0.0, 2.1, 0.8)


def _skellam_pmf(lam: float, mu: float, n: int) -> dict[int, float]:
    """P(X - Y = d) for X~Pois(lam), Y~Pois(mu), via 1-D convolution of the two
    marginals — independent of prob_margin's 2-D anti-diagonal sum."""
    xs = np.array([poisson_pmf(lam, k) for k in range(n + 1)])
    ys = np.array([poisson_pmf(mu, k) for k in range(n + 1)])
    conv = np.convolve(xs, ys[::-1])  # index i -> margin d = i - n
    conv /= conv.sum()  # match remaining_goal_matrix's truncate-then-renormalize
    return {i - n: float(conv[i]) for i in range(len(conv))}


def test_prob_margin_equals_skellam_at_rho_zero():
    """At rho=0 the joint matrix is a product of independent Poissons, so the margin
    distribution (supremacy) must match the Skellam pmf for every difference d."""
    lam, mu, n = 1.7, 1.1, 12
    m = remaining_goal_matrix(lam, mu, rho=0.0, max_goals=n)
    sk = _skellam_pmf(lam, mu, n)
    for d in range(-5, 6):
        assert abs(derived.prob_margin(m, d) - sk[d]) < 1e-9, d
    # prob_margin over all diffs sums to 1.
    total = sum(derived.prob_margin(m, d) for d in range(-n, n + 1))
    assert abs(total - 1.0) < 1e-9
