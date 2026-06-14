"""Calibration & evaluation.

Tracks model predictions against realized 1X2 outcomes for completed matches and
reports Brier score, multiclass log-loss, and a reliability table / Expected
Calibration Error (ECE). It also exposes ``calibration_factor()`` — a 0..1 Kelly
multiplier — so an *un*calibrated model is forced to size down (spec §4.5: "a model
that isn't calibrated must not size up").
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from ..models.schemas import Outcome, Probabilities

_EPS = 1e-12
_OUTCOME_ORDER = (Outcome.HOME, Outcome.DRAW, Outcome.AWAY)


@dataclass
class CalibrationTracker:
    """Accumulates (prediction, realized_outcome) pairs and scores them."""

    min_samples: int = 30
    ece_floor: float = 0.5  # Kelly multiplier when uncalibrated / too few samples
    preds: list[tuple[float, float, float]] = field(default_factory=list)
    actuals: list[tuple[int, int, int]] = field(default_factory=list)

    def add(self, probs: Probabilities, realized: Outcome) -> None:
        p = probs.normalized()
        self.preds.append((p.p_home, p.p_draw, p.p_away))
        self.actuals.append(tuple(1 if o is realized else 0 for o in _OUTCOME_ORDER))  # type: ignore[arg-type]

    @property
    def n(self) -> int:
        return len(self.preds)

    def _arrays(self) -> tuple[np.ndarray, np.ndarray]:
        return np.asarray(self.preds, dtype=float), np.asarray(self.actuals, dtype=float)

    def brier_score(self) -> float:
        """Mean multiclass Brier score (0 best, 2 worst)."""
        if self.n == 0:
            return float("nan")
        p, y = self._arrays()
        return float(np.mean(np.sum((p - y) ** 2, axis=1)))

    def log_loss(self) -> float:
        if self.n == 0:
            return float("nan")
        p, y = self._arrays()
        p = np.clip(p, _EPS, 1.0)
        return float(-np.mean(np.sum(y * np.log(p), axis=1)))

    def reliability_table(self, bins: int = 10) -> list[dict[str, float]]:
        """Pool all (predicted, realized) binary points across outcomes into bins."""
        if self.n == 0:
            return []
        p, y = self._arrays()
        preds = p.flatten()
        obs = y.flatten()
        edges = np.linspace(0.0, 1.0, bins + 1)
        table: list[dict[str, float]] = []
        for i in range(bins):
            lo, hi = edges[i], edges[i + 1]
            mask = (preds >= lo) & (preds < hi if i < bins - 1 else preds <= hi)
            count = int(mask.sum())
            if count == 0:
                continue
            table.append(
                {
                    "bin_low": float(lo),
                    "bin_high": float(hi),
                    "count": count,
                    "mean_predicted": float(preds[mask].mean()),
                    "empirical_freq": float(obs[mask].mean()),
                }
            )
        return table

    def ece(self, bins: int = 10) -> float:
        """Expected Calibration Error: weighted |predicted - empirical| over bins."""
        table = self.reliability_table(bins)
        if not table:
            return float("nan")
        total = sum(row["count"] for row in table)
        return sum(
            row["count"] / total * abs(row["mean_predicted"] - row["empirical_freq"])
            for row in table
        )

    def calibration_factor(self) -> float:
        """Kelly multiplier in [ece_floor, 1.0]. Conservative until proven calibrated."""
        if self.n < self.min_samples:
            return self.ece_floor
        e = self.ece()
        if np.isnan(e):
            return self.ece_floor
        # ECE of 0 -> 1.0; ECE of 0.10+ -> floor. Linear in between.
        scaled = 1.0 - (e / 0.10) * (1.0 - self.ece_floor)
        return float(min(1.0, max(self.ece_floor, scaled)))

    def metrics(self) -> dict[str, float]:
        return {
            "n": float(self.n),
            "brier": self.brier_score(),
            "log_loss": self.log_loss(),
            "ece": self.ece() if self.n else float("nan"),
            "calibration_factor": self.calibration_factor(),
        }
