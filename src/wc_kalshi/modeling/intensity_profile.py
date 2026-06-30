"""Within-match goal-intensity profile — how a match's goals split across its two halves.

Goals are not uniform over 90': scoring rises through the match, and the 2nd half outscores
the 1st (empirically ~45% / ~55% of goals in men's World Cups). This profile lets the
half-market heads (1st/2nd-half result, total, BTTS) derive from the SAME full-match scoring
rates as the rest of the board: split each team's full-match rate by the half weights, build a
per-half Poisson matrix, and the two halves convolve back to the full-time matrix (Poisson
additivity). Tunable; fit alongside the backbone when half-resolved data is available.
"""

from __future__ import annotations

# Fraction of full-match goals expected in the 1st half (the 2nd half gets the rest).
HALF1_FRACTION = 0.45


def half_weights() -> tuple[float, float]:
    """(1st-half, 2nd-half) fractions of the full-match scoring rate. They sum to 1, so a
    team's expected goals are preserved and the two half matrices convolve back to the
    full-time one (Poisson additivity)."""
    return HALF1_FRACTION, 1.0 - HALF1_FRACTION
