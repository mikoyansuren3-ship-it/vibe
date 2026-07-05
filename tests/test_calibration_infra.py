"""P0.3 calibration + de-vig infra: RPS, the binary calibration tracker, equal-count
reliability bins, and the 2-way (two-sided book) de-vig helper."""

import math
import random

from wc_kalshi.market.implied import implied_two_way
from wc_kalshi.modeling.calibration import BinaryCalibrationTracker, CalibrationTracker
from wc_kalshi.models.schemas import Outcome, Probabilities


def test_rps_rewards_ordinal_closeness():
    """RPS must penalise a 'far' miss (predict home, away wins) more than a 'near' miss
    (predict home, draw happens) — the ordinal property plain Brier/log-loss lack."""
    near = CalibrationTracker()
    far = CalibrationTracker()
    p = Probabilities(match_id="m", p_home=0.7, p_draw=0.2, p_away=0.1, source="model")
    near.add(p, Outcome.DRAW)  # adjacent in the home▸draw▸away ordering
    far.add(p, Outcome.AWAY)   # two steps away
    assert near.rps() < far.rps()
    # Sanity bounds: a confident correct call scores ~0; perfect prediction scores 0.
    perfect = CalibrationTracker()
    perfect.add(Probabilities(match_id="m", p_home=1.0, p_draw=0.0, p_away=0.0, source="model"), Outcome.HOME)
    assert perfect.rps() == 0.0
    assert "rps" in near.metrics()


def test_binary_tracker_scores_and_reliability():
    tr = BinaryCalibrationTracker(name="over_2.5", min_samples=50)
    rng = random.Random(1)
    # Perfectly calibrated: events at prob p occur with frequency p.
    for _ in range(4000):
        p = rng.random()
        tr.add(p, rng.random() < p)
    assert tr.n == 4000
    # Brier ≈ E[p(1-p)] ≈ 1/6 for uniform p; log-loss finite; ECE small (well-calibrated).
    assert 0.10 < tr.brier_score() < 0.22
    assert math.isfinite(tr.log_loss())
    assert tr.ece(equal_count=True) < 0.05
    # A well-calibrated head with enough samples sizes up above the floor.
    assert tr.calibration_factor() > tr.ece_floor
    m = tr.metrics()
    assert set(m) >= {"n", "brier", "log_loss", "ece", "calibration_factor"}


def test_binary_tracker_floor_when_thin():
    tr = BinaryCalibrationTracker(min_samples=30)
    tr.add(0.6, True)
    assert tr.calibration_factor() == tr.ece_floor  # too few samples -> conservative floor


def test_equal_count_bins_balance_counts():
    tr = BinaryCalibrationTracker()
    rng = random.Random(2)
    for _ in range(1000):
        p = rng.random() ** 3  # heavily skewed toward 0 -> fixed-width bins would be lopsided
        tr.add(p, rng.random() < p)
    eq = tr.reliability_table(bins=10, equal_count=True)
    fixed = tr.reliability_table(bins=10, equal_count=False)
    counts = [r["count"] for r in eq]
    # Equal-count bins are balanced (max within 1 of min); fixed-width are not on skewed data.
    assert max(counts) - min(counts) <= 1
    assert max(r["count"] for r in fixed) > max(counts)


def test_implied_two_way_fair_and_overround():
    # Symmetric mid is the fair prob; overround comes from the spread, not the mid.
    fair, over = implied_two_way(60, 64)
    assert abs(fair - 0.62) < 1e-9       # (60+64)/200
    assert abs(over - 1.04) < 1e-9       # 1 + (64-60)/100
    # One-sided book -> no fair price.
    assert implied_two_way(None, 64) == (None, 1.0)
    assert implied_two_way(60, None) == (None, 1.0)
    # Tighter spread -> overround closer to 1 (less cost to cross).
    _, tight = implied_two_way(61, 62)
    assert tight < over


def test_calibration_factor_is_memoized_until_add():
    """calibration_factor() is called on every sizing tick but only changes when a settled
    match is add()ed. It must reuse a cached value between settlements (no per-tick numpy
    rebuild) and invalidate the moment a new sample lands."""
    tr = CalibrationTracker(min_samples=5)
    rng = random.Random(7)
    for _ in range(50):
        p = rng.random()
        q = (1.0 - p) / 2.0
        tr.add(
            Probabilities(match_id="m", p_home=p, p_draw=q, p_away=q, source="model"),
            rng.choice([Outcome.HOME, Outcome.DRAW, Outcome.AWAY]),
        )

    calls = {"ece": 0}
    real_ece = tr.ece

    def counting_ece(*a, **k):
        calls["ece"] += 1
        return real_ece(*a, **k)

    tr.ece = counting_ece  # type: ignore[method-assign]

    first = tr.calibration_factor()
    assert calls["ece"] == 1  # computed once
    for _ in range(5):
        assert tr.calibration_factor() == first
    assert calls["ece"] == 1  # ...and never recomputed while the sample count is unchanged

    tr.add(
        Probabilities(match_id="m", p_home=0.5, p_draw=0.25, p_away=0.25, source="model"),
        Outcome.HOME,
    )
    again = tr.calibration_factor()
    assert calls["ece"] == 2  # add() invalidated the memo -> recomputed exactly once
    assert again == tr._calibration_factor()  # cached value is identical to a fresh compute


def test_metrics_is_memoized_and_copy_safe():
    """metrics() is polled by the dashboard every refresh; it must be memoized on the sample
    count and hand back a COPY so a caller mutating the dict can't poison the cache."""
    tr = CalibrationTracker(min_samples=5)
    rng = random.Random(3)
    for _ in range(20):
        p = rng.random()
        q = (1.0 - p) / 2.0
        tr.add(
            Probabilities(match_id="m", p_home=p, p_draw=q, p_away=q, source="model"),
            rng.choice([Outcome.HOME, Outcome.DRAW, Outcome.AWAY]),
        )

    m1 = tr.metrics()
    m2 = tr.metrics()
    assert m1 == m2 and m1 is not m2  # equal values, but distinct objects (defensive copy)
    m1["brier"] = -999.0  # a caller mutating the result must not leak into the cache
    assert tr.metrics()["brier"] != -999.0

    n_before = tr.metrics()["n"]
    tr.add(
        Probabilities(match_id="m", p_home=0.5, p_draw=0.25, p_away=0.25, source="model"),
        Outcome.HOME,
    )
    assert tr.metrics()["n"] == n_before + 1  # invalidated on add()
