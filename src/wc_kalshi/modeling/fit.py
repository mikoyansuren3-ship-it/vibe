"""Fit the model's behavioural constants against data instead of guessing them.

The critique was right that ``live_xg_weight``, ``red_card_xg_penalty``,
``leader_mult``/``chaser_mult`` and ``elo_tilt`` were hand-picked magic numbers. This
module turns them into *fitted* parameters: it samples model predictions at in-play
checkpoints and runs a cheap coordinate-descent grid search to minimise multiclass
log-loss against realised outcomes.

Feed it real historical matches (``backtest/historical.py``) for production fits. For
demonstration/CI it can also fit against the deterministic simulator — clearly NOT a
substitute for real data (the simulator is our own model of the world), but it proves
the machinery and gives sane, reproducible numbers.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from ..config import ModelSection
from ..models.schemas import MatchSnapshot, Outcome
from .inplay import DixonColesInplayModel

# Minutes at which we sample an in-play prediction for fitting.
FIT_CHECKPOINTS: tuple[int, ...] = (10, 25, 40, 55, 70, 80)

# Search grids per fittable constant (coordinate descent visits each in turn).
_GRIDS: dict[str, list[float]] = {
    "live_xg_weight": [0.3, 0.45, 0.6, 0.75, 0.9],
    "red_card_xg_penalty": [0.45, 0.55, 0.65, 0.75],
    "elo_tilt": [0.15, 0.2, 0.25, 0.3, 0.35],
    "leader_mult": [0.92, 0.95, 0.97, 1.0],
    "chaser_mult": [1.0, 1.03, 1.06, 1.1],
}


@dataclass
class FitResult:
    params: dict[str, float] = field(default_factory=dict)
    logloss_before: float = 0.0
    logloss_after: float = 0.0
    n_samples: int = 0

    def yaml_snippet(self) -> str:
        lines = ["model:"]
        for k, v in self.params.items():
            lines.append(f"  {k}: {round(v, 4)}")
        return "\n".join(lines)


def _realized(final: MatchSnapshot) -> Outcome:
    d = final.score_diff
    return Outcome.HOME if d > 0 else Outcome.DRAW if d == 0 else Outcome.AWAY


def _checkpoint_snaps(match: list[MatchSnapshot]) -> list[MatchSnapshot]:
    """Pick one live snapshot at/after each checkpoint minute."""
    out: list[MatchSnapshot] = []
    seen: set[int] = set()
    for snap in match:
        if not snap.period.is_live:
            continue
        for cp in FIT_CHECKPOINTS:
            if cp not in seen and snap.minute >= cp:
                seen.add(cp)
                out.append(snap)
    return out


def _eval_logloss(cfg: ModelSection, dataset: list[tuple[list[MatchSnapshot], Outcome]]) -> tuple[float, int]:
    model = DixonColesInplayModel(cfg)
    total = 0.0
    n = 0
    for snaps, outcome in dataset:
        for snap in snaps:
            p = model.predict(snap)
            pv = max(1e-6, min(1.0, p.get(outcome)))
            total += -math.log(pv)
            n += 1
    return (total / n if n else 0.0), n


def fit_constants(
    matches: list[list[MatchSnapshot]],
    base: ModelSection,
    *,
    passes: int = 2,
) -> FitResult:
    """Coordinate-descent fit of the behavioural constants minimising log-loss."""
    dataset: list[tuple[list[MatchSnapshot], Outcome]] = []
    for match in matches:
        if not match:
            continue
        dataset.append((_checkpoint_snaps(match), _realized(match[-1])))

    cfg = base.model_copy(deep=True)
    before, n = _eval_logloss(cfg, dataset)
    best = before
    for _ in range(max(1, passes)):
        for name, grid in _GRIDS.items():
            current = getattr(cfg, name)
            best_val = current
            for candidate in grid:
                trial = cfg.model_copy(update={name: candidate})
                ll, _ = _eval_logloss(trial, dataset)
                if ll < best:
                    best = ll
                    best_val = candidate
            cfg = cfg.model_copy(update={name: best_val})

    params = {k: getattr(cfg, k) for k in _GRIDS}
    return FitResult(params=params, logloss_before=before, logloss_after=best, n_samples=n)
