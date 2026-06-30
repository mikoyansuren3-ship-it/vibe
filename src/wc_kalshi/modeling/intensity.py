"""Goal-intensity shaping for the Dixon-Coles backbone (modeling program P0.1).

Pulls the rate-projection knobs out of ``inplay.py`` so they can be unit-tested and fit
independently (modeling/fit.py, plan P0.4). Every default here REPRODUCES the prior
hard-coded behaviour exactly, so the model only changes once these are fit and the
held-out A/B confirms the upgrade — never silently.

The four levers, each a no-op at its default:
  * ``remaining_fraction``  — a time-inhomogeneous goal profile (goals rise late);
  * ``credibility_weight``  — info-weighted live-xG shrinkage (vs the flat elapsed/90);
  * ``score_state_mults``   — graded leader/chaser multipliers by margin;
  * ``red_card_factors``    — asymmetric own-penalty / opponent-boost per red card.
"""

from __future__ import annotations


def remaining_fraction(elapsed: float, slope: float = 0.0, total: float = 90.0) -> float:
    """Fraction of a match's goal expectation still to come at ``elapsed`` minutes.

    Models intensity as g(s) = 1 + slope·(2s/total − 1), mean-preserving over [0, total],
    so ``slope`` reshapes WHEN goals are expected without changing the full-match total.
    Closed form: f_rem = (1−u)·(1 + slope·u), u = elapsed/total.

    ``slope=0`` is the flat (1−u) legacy profile (so f_rem·total = remaining minutes);
    ``slope in (0,1)`` makes goals arrive later (more of the match's goals still to come
    at half-time). Requires slope < 1 to keep the intensity positive at kickoff.
    """
    u = min(1.0, max(0.0, elapsed / total))
    return (1.0 - u) * (1.0 + slope * u)


def credibility_weight(x_observed: float, k: float) -> float:
    """Empirical-Bayes weight on the observed (live) rate vs the prior: w = X/(X+k).

    ``X`` is accumulated information (cumulative live xG) and ``k`` the prior strength in
    xG units. A quiet 0-0 (little xG) keeps the Elo prior; a high-xG game trusts the live
    signal — unlike a flat elapsed/90 weight, which over-trusts a late low-xG game.
    """
    if x_observed <= 0.0 or k <= 0.0:
        return 0.0
    return x_observed / (x_observed + k)


def score_state_mults(
    diff: int, *, leader_mult: float, chaser_mult: float, per_goal: float = 0.0
) -> tuple[float, float]:
    """(home_mult, away_mult) on the remaining rates given the current (home−away) lead.

    Graded by the lead size: each extra goal of lead deepens the effect by ``per_goal``
    (chaser pushes harder when 2 down, leader sits deeper). ``per_goal=0`` reproduces the
    flat single-multiplier legacy behaviour for every margin.
    """
    if diff == 0:
        return 1.0, 1.0
    scale = 1.0 + per_goal * (abs(diff) - 1)
    # max(0.4, …) floors the leader multiplier so a graded multi-goal lead can't drive the
    # remaining rate to ~0. Only reachable when per_goal>0 (non-default); at per_goal=0,
    # scale==1 and lead_m==leader_mult, so the floor never bites and behaviour is legacy.
    lead_m = max(0.4, 1.0 - (1.0 - leader_mult) * scale)
    chase_m = 1.0 + (chaser_mult - 1.0) * scale
    return (lead_m, chase_m) if diff > 0 else (chase_m, lead_m)


def red_card_factors(penalty: float, opponent_boost: float | None) -> tuple[float, float]:
    """(own_factor, opponent_factor) applied per red card to the remaining rates.

    ``opponent_boost=None`` reproduces the legacy symmetric boost ``1 + (1 − penalty)``;
    set it explicitly for an asymmetric shock (a sending-off hurts the carded side more
    than it helps the opponent).
    """
    boost = opponent_boost if opponent_boost is not None else 1.0 + (1.0 - penalty)
    return penalty, boost
