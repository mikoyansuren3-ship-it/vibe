"""Per-outcome calibration diagnostic: the pooled ECE averages the three binary problems
together, so systematic per-class bias can cancel. per_outcome_metrics exposes it."""

import random

from wc_kalshi.modeling.calibration import CalibrationTracker
from wc_kalshi.models.schemas import Outcome, Probabilities


def test_per_outcome_metrics_exposes_class_bias():
    tr = CalibrationTracker()
    rng = random.Random(0)
    # Model always says home 0.60, but home actually wins ~40% — a clear over-prediction
    # that a pooled metric would partly mask.
    for i in range(300):
        p = Probabilities(match_id=f"m{i}", p_home=0.60, p_draw=0.25, p_away=0.15, source="model")
        r = Outcome.HOME if rng.random() < 0.40 else (Outcome.DRAW if rng.random() < 0.5 else Outcome.AWAY)
        tr.add(p, r)

    po = tr.per_outcome_metrics()
    assert set(po) == {"home", "draw", "away"}
    assert po["home"]["bias"] > 0.1  # predicted 0.60 >> empirical ~0.40
    assert po["home"]["mean_predicted"] == 0.60
    assert "ece" in po["home"] and po["home"]["ece"] >= 0.0


def test_per_outcome_metrics_empty_when_no_data():
    assert CalibrationTracker().per_outcome_metrics() == {}
