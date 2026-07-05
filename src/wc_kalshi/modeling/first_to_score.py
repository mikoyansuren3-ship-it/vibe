"""First-to-score market — a bespoke head that stays coherent with the backbone (plan P3).

The scoreline matrix ``M`` has no *timing* dimension, so "which team scores first?" cannot
be a sum of its cells. But it does not need a new model either: the same remaining-goal
rates ``(λ_rem, μ_rem)`` that build ``M`` define two competing homogeneous Poisson goal
processes over the rest of the match, and the first-passage split is closed-form

    Λ = λ_rem + μ_rem
    P(home first) = (λ_rem / Λ) · (1 − e^(−Λ))
    P(away first) = (μ_rem / Λ) · (1 − e^(−Λ))
    P(no goal)    = e^(−Λ)

Reusing the backbone rates (not a fresh fit) keeps this head consistent with every
scoreline-derived market: ``P(no goal)`` here is exactly the remaining-goal matrix's
``[0, 0]`` cell at ρ=0. The within-window time profile ``g(t)`` cancels out of the split
(both processes share it), and only enters through the integral Λ — so the formula stays
exact even with ``goal_time_slope ≠ 0``.

**Settlement / collapse.** The market resolves on the match's *first* goal, so once any
goal is in, the live projection is moot:
  * exactly one side has scored → that side scored first (degenerate);
  * the match ended 0-0 → "no goal" (degenerate);
  * BOTH sides have scored → the order is not recoverable from the score alone. Pass the
    append-only tick stream to recover the first scorer; if even that can't disambiguate
    (two goals inside one capture gap) the head **refuses to price** (``ambiguous``) rather
    than guess — never trade a market you can't settle.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Sequence

from ..models.schemas import MatchSnapshot

if TYPE_CHECKING:  # avoid a runtime import cycle (inplay imports nothing from here)
    from .inplay import DixonColesInplayModel, PricedSnapshot

# Below this combined remaining rate the match is effectively over: treat it as goalless to
# avoid a 0/0 in the rate split (Λ→0 ⇒ P(no goal)→1).
_MIN_LAMBDA = 1e-9


@dataclass(frozen=True)
class FirstToScore:
    """First-to-score probabilities for a match's *first* goal.

    ``p_home``/``p_away``/``p_no_goal`` are ``None`` exactly when ``ambiguous`` — both teams
    have scored but the order is unknown, so the market is unpriceable. ``settled`` means the
    first goal (or a goalless full time) has already fixed the result; the probabilities are
    then degenerate (a 1 and two 0s, or all-``None`` if ambiguous)."""

    p_home: float | None
    p_away: float | None
    p_no_goal: float | None
    settled: bool
    ambiguous: bool = False

    @property
    def tradeable(self) -> bool:
        """False when both teams have scored and the order can't be recovered."""
        return not self.ambiguous

    def as_dict(self) -> dict[str, float | None | bool]:
        return {
            "home": self.p_home,
            "away": self.p_away,
            "no_goal": self.p_no_goal,
            "settled": self.settled,
            "ambiguous": self.ambiguous,
        }


def first_to_score_rates(lam: float, mu: float) -> tuple[float, float, float]:
    """``(P(home first), P(away first), P(no goal))`` from competing Poisson remaining rates.

    ``lam``/``mu`` are the *remaining*-goal expectations (``DixonColesInplayModel.remaining_rates``),
    not per-minute rates. The triple sums to 1. Uses ``expm1`` so ``1 − e^(−Λ)`` keeps its
    precision when Λ is small (a late, quiet game)."""
    lam = max(0.0, lam)
    mu = max(0.0, mu)
    total = lam + mu
    if total <= _MIN_LAMBDA:
        return 0.0, 0.0, 1.0
    p_any = -math.expm1(-total)  # 1 − e^(−Λ), accurate for small Λ
    p_no_goal = math.exp(-total)
    p_home = (lam / total) * p_any
    p_away = (mu / total) * p_any
    return p_home, p_away, p_no_goal


def first_scorer_from_ticks(history: Sequence[MatchSnapshot] | None) -> str | None:
    """Recover which side scored the match's first goal from the append-only tick stream.

    Returns ``"home"``/``"away"`` when the earliest goal-bearing snapshot pins the scorer, or
    ``None`` when it can't — no goals captured yet, OR both sides are already on the board in
    the first goal-bearing snapshot (two goals landed inside one capture gap, order lost).
    Assumes the snapshots are in capture order (as stored)."""
    if not history:
        return None
    for snap in history:
        hs, as_ = snap.home_score, snap.away_score
        if hs > 0 and as_ == 0:
            return "home"
        if as_ > 0 and hs == 0:
            return "away"
        if hs > 0 and as_ > 0:
            return None  # first goal-bearing tick already has both — order unrecoverable
    return None


def _settled(scorer: str | None) -> FirstToScore:
    return FirstToScore(
        p_home=1.0 if scorer == "home" else 0.0,
        p_away=1.0 if scorer == "away" else 0.0,
        p_no_goal=0.0,
        settled=True,
    )


def first_to_score(
    model: "DixonColesInplayModel",
    match: MatchSnapshot,
    history: Sequence[MatchSnapshot] | None = None,
    *,
    priced: "PricedSnapshot | None" = None,
) -> FirstToScore:
    """First-to-score probabilities for ``match``, collapsing once the first goal is in.

    ``history`` (the match's snapshot stream) is only consulted to disambiguate a game where
    both teams have already scored; for a 0-0 (live or pre-kickoff) game it is unused and the
    live projection is priced off the backbone remaining rates. ``priced`` (optional) reuses
    the shared backbone rates instead of recomputing them."""
    hs, as_ = match.home_score, match.away_score

    if hs > 0 or as_ > 0:  # a goal is in — the market is (or should be) settled
        if hs > 0 and as_ == 0:
            return _settled("home")
        if as_ > 0 and hs == 0:
            return _settled("away")
        scorer = first_scorer_from_ticks(history)  # both scored — need the order
        if scorer is not None:
            return _settled(scorer)
        return FirstToScore(None, None, None, settled=True, ambiguous=True)

    # Still 0-0.
    if match.period.is_finished or match.status == "finished":
        return FirstToScore(0.0, 0.0, 1.0, settled=True)  # ended goalless

    lam, mu = model.remaining_rates(match, priced=priced)
    p_home, p_away, p_no_goal = first_to_score_rates(lam, mu)
    return FirstToScore(p_home, p_away, p_no_goal, settled=False)
