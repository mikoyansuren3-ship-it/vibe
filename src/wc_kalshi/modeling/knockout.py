"""Knockout-stage market probabilities — to-advance, method of victory, go-to-ET/penalties.

A knockout game can't end level: a tie at 90' goes to extra time (a 30' phase), and a tie
after extra time goes to a penalty shootout. We compose two scoreline matrices — the 90'
regulation matrix and a separate extra-time matrix (``DixonColesInplayModel.scoreline_matrix_et``)
— plus a shootout probability, into

    P(advance) = P(win reg) + P(tie reg) · [P(win ET) + P(tie ET) · P(win shootout)]

and the method-of-victory decomposition (win in regulation / extra time / penalties). Kalshi
trades only "to advance" (KXWCADVANCE, includes ET + penalties); the rest are model-only
projections. The shootout is a coin flip (0.50) — World Cup knockouts are neutral-venue and
historical shootouts are ~random; the user chose this over an Elo tilt. Tunable here.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from ..models.schemas import MatchSnapshot
from .inplay import DixonColesInplayModel, PricedSnapshot

# P(home wins the shootout | level after extra time). Coin flip by default.
PENS_HOME_WIN = 0.5
# How many of the most-likely extra-time scorelines to surface.
ET_TOP_N = 6


def _win_probs(m: np.ndarray) -> tuple[float, float, float]:
    """Collapse a joint score matrix into (home win, draw, away win). Inlined (not imported
    from backtest/export) to keep modeling/ free of a backtest dependency."""
    # Vectorized sign-collapse on (i−j) — index-grid masks instead of the O(G²) Python loop.
    # Matches the loop to ~1e-15 (inside the 1e-12 golden tolerance).
    n_h, n_a = m.shape
    diff = np.arange(n_h)[:, None] - np.arange(n_a)[None, :]
    return (
        float(m[diff > 0].sum()),
        float(m[diff == 0].sum()),
        float(m[diff < 0].sum()),
    )


def pens_home_win(match: MatchSnapshot) -> float:
    """P(home wins the shootout | level after extra time). A coin flip by default (neutral
    venue + near-random shootouts); isolated here so it is trivially tunable to a capped
    Elo tilt later without touching the composition."""
    return PENS_HOME_WIN


def knockout_breakdown(
    model: DixonColesInplayModel, match: MatchSnapshot, *, priced: PricedSnapshot | None = None
) -> dict[str, Any]:
    """Full knockout decomposition from the regulation + extra-time matrices.

    Returns ``advance`` (per team, sums to 1), the per-team method-of-victory split
    (win in regulation / extra time / penalties — which sum to ``advance``), ``go_to_extra_time``
    / ``go_to_penalties``, and the top-N extra-time scorelines. Conditional only on the
    current match state. ``priced`` (optional) supplies the shared regulation matrix; the
    extra-time matrix is knockout-specific and always built here."""
    m_reg = model.scoreline_matrix(match, priced=priced)
    ph_reg, p_tie_reg, pa_reg = _win_probs(m_reg)

    m_et = model.scoreline_matrix_et(match)
    ph_et, p_tie_et, pa_et = _win_probs(m_et)

    pso_home = pens_home_win(match)
    pso_away = 1.0 - pso_home

    win_reg = (ph_reg, pa_reg)
    win_et = (p_tie_reg * ph_et, p_tie_reg * pa_et)
    win_pens = (p_tie_reg * p_tie_et * pso_home, p_tie_reg * p_tie_et * pso_away)
    advance = (win_reg[0] + win_et[0] + win_pens[0], win_reg[1] + win_et[1] + win_pens[1])

    cells = sorted(
        (((i, j), float(m_et[i, j])) for i in range(m_et.shape[0]) for j in range(m_et.shape[1])),
        key=lambda c: c[1], reverse=True,
    )[:ET_TOP_N]

    return {
        "advance": advance,
        "win_regulation": win_reg,
        "win_extra_time": win_et,
        "win_penalties": win_pens,
        "go_to_extra_time": p_tie_reg,
        "go_to_penalties": p_tie_reg * p_tie_et,
        "et_scorelines": [((i, j), prob) for (i, j), prob in cells],
    }


def advance_probs(model: DixonColesInplayModel, match: MatchSnapshot) -> tuple[float, float]:
    """(P(home advances), P(away advances)) including extra time + penalties. Sums to 1."""
    return knockout_breakdown(model, match)["advance"]
