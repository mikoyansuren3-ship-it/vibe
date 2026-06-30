"""The P0.4 harness's pre-registered acceptance gate (scripts/fit_backbone.py).

The harness ships an intensity-knob flip only if it beats baseline on held-out 1X2 (log-loss
AND RPS) AND does not worsen the traded scoreline-derived heads beyond tolerance. This pins the
DOWNSTREAM half of that gate — the part added after a 1X2-only run nearly shipped a flip that
degraded Total Over 3.5 — so the clause can't silently regress. Pure logic only; no fitting."""

import pathlib
import sys

import numpy as np
import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
import fit_backbone as fb  # noqa: E402


def _down(o15=0.20, o25=0.24, o35=0.10, sup=0.15) -> dict:
    """A downstream-metrics fold dict in the shape eval_downstream returns."""
    return {"o1.5_brier": o15, "o2.5_brier": o25, "o3.5_brier": o35, "btts_brier": 0.24, "sup_rps": sup}


def test_gate_tolerances_are_the_preregistered_values():
    # Guards against an accidental loosening of the committed thresholds.
    assert fb.DOWNSTREAM_BRIER_TOL == 0.005
    assert fb.DOWNSTREAM_RPS_TOL == 0.001


def test_clean_arm_passes_downstream():
    base = [_down(), _down()]
    arm = [_down(o35=0.095), _down(o35=0.098)]  # strictly better everywhere
    ok, reasons = fb._passes_downstream(arm, base)
    assert ok and reasons == []


def test_o35_regression_in_one_fold_fails():
    # The real slope_only failure mode: O3.5 Brier worsens +0.0079 in the second fold.
    base = [_down(o35=0.100), _down(o35=0.100)]
    arm = [_down(o35=0.1023), _down(o35=0.1079)]  # +0.0023 (ok) then +0.0079 (> 0.005)
    ok, reasons = fb._passes_downstream(arm, base)
    assert not ok
    assert any("O3.5" in r and "fold1" in r for r in reasons)


def test_supremacy_rps_regression_fails():
    base = [_down(sup=0.150), _down(sup=0.150)]
    arm = [_down(sup=0.152), _down(sup=0.152)]  # +0.002 pooled > 0.001
    ok, reasons = fb._passes_downstream(arm, base)
    assert not ok
    assert any("supremacy" in r for r in reasons)


def test_brier_boundary_is_inclusive_of_tolerance():
    base = [_down(o25=0.200), _down(o25=0.200)]
    at_tol = [_down(o25=0.205), _down(o25=0.205)]      # exactly +0.005 → allowed (not > tol)
    over_tol = [_down(o25=0.205), _down(o25=0.20501)]  # +0.00501 in fold1 → fails
    assert fb._passes_downstream(at_tol, base)[0]
    assert not fb._passes_downstream(over_tol, base)[0]


def test_ordered_rps_zero_for_perfect_and_positive_for_miss():
    p_perfect = np.array([0.0, 0.0, 1.0, 0.0, 0.0])
    assert fb._ordered_rps(p_perfect, 2) == pytest.approx(0.0)
    # All mass on the far-away bucket vs a draw outcome → strictly positive, worse than a near miss.
    p_far = np.array([1.0, 0.0, 0.0, 0.0, 0.0])
    p_near = np.array([0.0, 1.0, 0.0, 0.0, 0.0])
    assert fb._ordered_rps(p_far, 2) > fb._ordered_rps(p_near, 2) > 0.0


def test_margin_bucket_partitions_the_line():
    assert [fb._margin_bucket(d) for d in (-3, -2, -1, 0, 1, 2, 5)] == \
        ["A2", "A2", "A1", "D", "H1", "H2", "H2"]


def test_supremacy_bucket_probs_normalize():
    m = np.array([[0.2, 0.1, 0.05], [0.15, 0.2, 0.05], [0.1, 0.05, 0.1]])
    v = fb._supremacy_bucket_probs(m)
    assert len(v) == 5 and v.sum() == pytest.approx(1.0)
