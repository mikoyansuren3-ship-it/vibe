"""Scoreline-derived market probabilities (roadmap Tier 1).

Pure functions over a joint final-score matrix ``M[i, j] = P(home_final=i, away_final=j)``
(from ``DixonColesInplayModel.scoreline_matrix``). Every per-match scoreline market — total
goals, spread/handicap, both-teams-to-score, correct score, team total, winning margin — is
a sum over cells of ``M``. No new model; the model already produces ``M``.

Lines follow Kalshi's structured strikes (``floor_strike``, e.g. 2.5 for "Over 2.5"), so an
"over N.5" market means ``> floor_strike`` (strictly greater).
"""

from __future__ import annotations

import numpy as np

Side = str  # "home" | "away"


def total_goals_pmf(m: np.ndarray) -> np.ndarray:
    """P(total goals = k) for k = 0 .. (max_home+max_away)."""
    n_h, n_a = m.shape
    pmf = np.zeros(n_h + n_a - 1)
    for i in range(n_h):
        for j in range(n_a):
            pmf[i + j] += m[i, j]
    return pmf


def prob_total_over(m: np.ndarray, line: float) -> float:
    """P(home+away goals > line). ``line`` is the .5 strike (e.g. 2.5)."""
    pmf = total_goals_pmf(m)
    k = int(np.floor(line)) + 1  # first integer total strictly above the line
    return float(pmf[k:].sum()) if k < len(pmf) else 0.0


def prob_total_under(m: np.ndarray, line: float) -> float:
    return 1.0 - prob_total_over(m, line)


def prob_btts(m: np.ndarray) -> float:
    """P(both teams score) = P(home>=1 and away>=1)."""
    return float(m[1:, 1:].sum())


def prob_correct_score(m: np.ndarray, home: int, away: int) -> float:
    if 0 <= home < m.shape[0] and 0 <= away < m.shape[1]:
        return float(m[home, away])
    return 0.0


def _team_marginal(m: np.ndarray, side: Side) -> np.ndarray:
    """P(side scores k goals)."""
    return m.sum(axis=1) if side == "home" else m.sum(axis=0)


def prob_team_total_over(m: np.ndarray, side: Side, line: float) -> float:
    """P(one team's goals > line), e.g. 'Argentina over 1.5'."""
    marg = _team_marginal(m, side)
    k = int(np.floor(line)) + 1
    return float(marg[k:].sum()) if k < len(marg) else 0.0


def supremacy_pmf(m: np.ndarray) -> dict[int, float]:
    """P(home_goals − away_goals = d) for every margin d, as ``{d: prob}``. The full goal
    supremacy distribution — the basis for margin / spread / Asian-handicap pricing (each is
    a sum over the relevant margins). Generalises ``prob_margin``."""
    n_h, n_a = m.shape
    out: dict[int, float] = {}
    for i in range(n_h):
        for j in range(n_a):
            d = i - j
            out[d] = out.get(d, 0.0) + float(m[i, j])
    return out


def prob_spread(m: np.ndarray, side: Side, line: float) -> float:
    """P(side wins by more than ``line`` goals), e.g. 'Argentina wins by more than 1.5'."""
    pmf = supremacy_pmf(m)
    return float(sum(p for d, p in pmf.items() if (d if side == "home" else -d) > line))


def prob_margin(m: np.ndarray, margin: int) -> float:
    """P(home_score − away_score == margin) (margin may be negative = away win)."""
    return supremacy_pmf(m).get(margin, 0.0)
