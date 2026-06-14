"""Probability-model interface + factory.

Any model maps a ``MatchSnapshot`` -> normalized ``Probabilities`` (1X2). Keeping
this an interface means the Dixon-Coles baseline can be swapped for an ML model
later without touching the edge/sizing/execution stages.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from ..models.schemas import MatchSnapshot, Probabilities

if TYPE_CHECKING:
    from ..config import AppConfig


class ProbabilityModel(ABC):
    name: str = "base"

    @abstractmethod
    def predict(self, match: MatchSnapshot) -> Probabilities:
        """Return a normalized 1X2 probability for the FULL-TIME (90') result."""


def build_model(cfg: "AppConfig") -> ProbabilityModel:
    name = cfg.model.name.lower()
    if name in {"dixon_coles_inplay", "dixon_coles", "baseline"}:
        from .inplay import DixonColesInplayModel

        return DixonColesInplayModel(cfg.model)
    raise ValueError(f"Unknown model: {cfg.model.name!r}")
