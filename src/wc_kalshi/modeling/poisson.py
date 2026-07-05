"""Poisson / Dixon-Coles scoreline mathematics.

Pure, dependency-light (numpy) functions for:
  * the Dixon-Coles low-score dependence correction ``tau``,
  * the joint distribution over *remaining* goals,
  * collapsing that into 1X2 (home/draw/away) given the current score.

Kept separate from the model class so the math is independently unit-tested.
"""

from __future__ import annotations

import math

import numpy as np


def poisson_pmf(lam: float, k: int) -> float:
    if lam < 0:
        lam = 0.0
    if lam == 0:
        return 1.0 if k == 0 else 0.0
    return math.exp(-lam) * lam**k / math.factorial(k)


def dc_tau(x: int, y: int, lam: float, mu: float, rho: float) -> float:
    """Dixon-Coles correction for the dependence at low scorelines."""
    if x == 0 and y == 0:
        return 1.0 - lam * mu * rho
    if x == 0 and y == 1:
        return 1.0 + lam * rho
    if x == 1 and y == 0:
        return 1.0 + mu * rho
    if x == 1 and y == 1:
        return 1.0 - rho
    return 1.0


def remaining_goal_matrix(
    lam: float, mu: float, *, rho: float = 0.0, max_goals: int = 12
) -> np.ndarray:
    """Joint pmf P(remaining_home=x, remaining_away=y), x,y in [0, max_goals].

    Applies the Dixon-Coles correction to the low-score cells and renormalizes so
    the (truncated) matrix sums to 1.
    """
    xs = np.array([poisson_pmf(lam, k) for k in range(max_goals + 1)])
    ys = np.array([poisson_pmf(mu, k) for k in range(max_goals + 1)])
    matrix = np.outer(xs, ys)
    for x in range(min(2, max_goals + 1)):
        for y in range(min(2, max_goals + 1)):
            matrix[x, y] *= dc_tau(x, y, lam, mu, rho)
    total = matrix.sum()
    if total > 0:
        matrix /= total
    return matrix


def outcome_probs(matrix: np.ndarray, current_diff: int = 0) -> tuple[float, float, float]:
    """Collapse a remaining-goal matrix into (P_home, P_draw, P_away).

    ``current_diff`` = current (home - away) score; the final margin is
    ``current_diff + x - y``.
    """
    # Vectorized collapse: build the final-margin grid once and sum by sign, instead of an
    # O(G²) Python loop. `fit_constants` re-runs the predict path ~66× per checkpoint, so this
    # pays 10–50× there; matches the loop to ~1e-15 (well inside the 1e-12 golden tolerance).
    n = matrix.shape[0]
    idx = np.arange(n)
    final = current_diff + idx[:, None] - idx[None, :]
    return (
        float(matrix[final > 0].sum()),
        float(matrix[final == 0].sum()),
        float(matrix[final < 0].sum()),
    )


def one_x_two(
    lam_rem: float,
    mu_rem: float,
    current_diff: int = 0,
    *,
    rho: float = 0.0,
    max_goals: int = 12,
) -> tuple[float, float, float]:
    """Convenience: remaining rates + current score -> normalized 1X2."""
    matrix = remaining_goal_matrix(lam_rem, mu_rem, rho=rho, max_goals=max_goals)
    return outcome_probs(matrix, current_diff)
