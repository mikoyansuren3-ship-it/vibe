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
    # (n_at_compute, factor) memo — calibration_factor() is called once per actionable edge
    # (every sizing tick) but only changes when a settled match is add()ed, so we key the
    # cache on the sample count and rebuild the O(history) arrays + binning only then.
    _cf_cache: tuple[int, float] | None = field(default=None, init=False, repr=False, compare=False)

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

    def rps(self) -> float:
        """Mean Ranked Probability Score for the ORDERED 1X2 (home ▸ draw ▸ away). A proper
        ORDINAL score (Constantinou-Fenton 2012): a prediction that is 'far' from the outcome
        in the ordering is penalised more than a near miss — which plain Brier/log-loss treat
        alike. 0 is best. Use alongside log-loss to judge the 1X2 / supremacy heads."""
        if self.n == 0:
            return float("nan")
        p, y = self._arrays()
        cum_p = np.cumsum(p, axis=1)[:, :-1]  # drop the last cumulative (always 1)
        cum_y = np.cumsum(y, axis=1)[:, :-1]
        return float(np.mean(np.sum((cum_p - cum_y) ** 2, axis=1) / (p.shape[1] - 1)))

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

    @staticmethod
    def _binned_ece(preds: np.ndarray, obs: np.ndarray, bins: int) -> float:
        if preds.size == 0:
            return float("nan")
        edges = np.linspace(0.0, 1.0, bins + 1)
        total = preds.size
        acc = 0.0
        for i in range(bins):
            lo, hi = edges[i], edges[i + 1]
            mask = (preds >= lo) & (preds < hi if i < bins - 1 else preds <= hi)
            c = int(mask.sum())
            if c:
                acc += c / total * abs(preds[mask].mean() - obs[mask].mean())
        return acc

    def per_outcome_metrics(self, bins: int = 10) -> dict[str, dict[str, float]]:
        """Per-CLASS calibration (home / draw / away). The pooled ECE averages the three
        binary problems together, so a systematic +home / −draw bias can cancel to ≈0 even
        when each class is mis-calibrated. This exposes each class's mean predicted vs
        empirical, its signed bias, and its own ECE. DIAGNOSTIC ONLY — deliberately not fed
        into sizing on these small per-class samples."""
        if self.n == 0:
            return {}
        p, y = self._arrays()
        out: dict[str, dict[str, float]] = {}
        for i, o in enumerate(_OUTCOME_ORDER):
            preds, obs = p[:, i], y[:, i]
            out[o.value] = {
                "mean_predicted": float(preds.mean()),
                "empirical_freq": float(obs.mean()),
                "bias": float(preds.mean() - obs.mean()),
                "ece": self._binned_ece(preds, obs, bins),
            }
        return out

    def calibration_factor(self) -> float:
        """Kelly multiplier in [ece_floor, 1.0]. Conservative until proven calibrated.
        Memoized on the sample count (see ``_cf_cache``) — identical result, but no
        per-tick numpy rebuild between settlements."""
        if self._cf_cache is not None and self._cf_cache[0] == self.n:
            return self._cf_cache[1]
        val = self._calibration_factor()
        self._cf_cache = (self.n, val)
        return val

    def _calibration_factor(self) -> float:
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
            "rps": self.rps() if self.n else float("nan"),
            "ece": self.ece() if self.n else float("nan"),
            "calibration_factor": self.calibration_factor(),
        }


def _binned_reliability(
    preds: np.ndarray, obs: np.ndarray, bins: int, equal_count: bool
) -> list[dict[str, float]]:
    """Reliability table over (predicted, realized∈{0,1}) points. ``equal_count=True`` uses
    equal-sample-count (quantile) bins — robust for thin/skewed binary heads where fixed-width
    bins leave most cells near-empty; ``False`` uses fixed-width [0,1] bins (matches the 1X2
    tracker's pooled table)."""
    if preds.size == 0:
        return []
    table: list[dict[str, float]] = []
    if equal_count:
        order = np.argsort(preds, kind="mergesort")
        for idx in np.array_split(order, bins):
            if idx.size == 0:
                continue
            pm, om = preds[idx], obs[idx]
            table.append({
                "bin_low": float(pm.min()), "bin_high": float(pm.max()), "count": int(idx.size),
                "mean_predicted": float(pm.mean()), "empirical_freq": float(om.mean()),
            })
        return table
    edges = np.linspace(0.0, 1.0, bins + 1)
    for i in range(bins):
        lo, hi = edges[i], edges[i + 1]
        mask = (preds >= lo) & (preds < hi if i < bins - 1 else preds <= hi)
        c = int(mask.sum())
        if c:
            table.append({
                "bin_low": float(lo), "bin_high": float(hi), "count": c,
                "mean_predicted": float(preds[mask].mean()), "empirical_freq": float(obs[mask].mean()),
            })
    return table


@dataclass
class BinaryCalibrationTracker:
    """Calibration for a BINARY market head — Over/Under per line, BTTS, to-advance,
    goes-to-ET, first-to-score, etc. The same Brier / log-loss / reliability / ECE machinery
    as the 1X2 ``CalibrationTracker`` but on ``(predicted_prob, realized 0/1)`` pairs, so every
    derived and knockout head can be judged honestly from day one (plan P0.3). ``name`` labels
    which market/line it tracks. Sizing still defaults to the conservative ECE→factor gate;
    rewiring ``calibration_factor`` to an out-of-sample market-skill ratio is deferred until
    the labelled (pred+quote+outcome) panel is rebuilt (honest-backtest caveat)."""

    name: str = ""
    min_samples: int = 30
    ece_floor: float = 0.5
    preds: list[float] = field(default_factory=list)
    actuals: list[int] = field(default_factory=list)
    # (n_at_compute, equal_count, factor) memo — see CalibrationTracker._cf_cache.
    _cf_cache: tuple[int, bool, float] | None = field(default=None, init=False, repr=False, compare=False)

    def add(self, prob: float, realized: bool) -> None:
        self.preds.append(min(1.0, max(0.0, float(prob))))
        self.actuals.append(1 if realized else 0)

    @property
    def n(self) -> int:
        return len(self.preds)

    def _arrays(self) -> tuple[np.ndarray, np.ndarray]:
        return np.asarray(self.preds, dtype=float), np.asarray(self.actuals, dtype=float)

    def brier_score(self) -> float:
        if self.n == 0:
            return float("nan")
        p, y = self._arrays()
        return float(np.mean((p - y) ** 2))

    def log_loss(self) -> float:
        if self.n == 0:
            return float("nan")
        p, y = self._arrays()
        p = np.clip(p, _EPS, 1.0 - _EPS)
        return float(-np.mean(y * np.log(p) + (1.0 - y) * np.log(1.0 - p)))

    def reliability_table(self, bins: int = 10, *, equal_count: bool = False) -> list[dict[str, float]]:
        p, y = self._arrays()
        return _binned_reliability(p, y, bins, equal_count)

    def ece(self, bins: int = 10, *, equal_count: bool = False) -> float:
        table = self.reliability_table(bins, equal_count=equal_count)
        if not table:
            return float("nan")
        total = sum(r["count"] for r in table)
        return sum(
            r["count"] / total * abs(r["mean_predicted"] - r["empirical_freq"]) for r in table
        )

    def calibration_factor(self, *, equal_count: bool = True) -> float:
        """Kelly multiplier in [ece_floor, 1.0]. Conservative until proven calibrated; uses
        equal-count bins by default since binary heads are often thin/skewed. Memoized on
        (sample count, equal_count) — see CalibrationTracker.calibration_factor."""
        if (
            self._cf_cache is not None
            and self._cf_cache[0] == self.n
            and self._cf_cache[1] == equal_count
        ):
            return self._cf_cache[2]
        val = self._calibration_factor(equal_count=equal_count)
        self._cf_cache = (self.n, equal_count, val)
        return val

    def _calibration_factor(self, *, equal_count: bool) -> float:
        if self.n < self.min_samples:
            return self.ece_floor
        e = self.ece(equal_count=equal_count)
        if np.isnan(e):
            return self.ece_floor
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
