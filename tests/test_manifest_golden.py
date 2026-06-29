"""Golden-numbers regression guard for the PUBLISHED manifest.

The web app ships ``web/public/bundles/manifest.json`` as the canonical, citable
headline (CLV, calibration, P&L). Those numbers are RECOMPUTED with current model
code at export time, so a quiet change to ``xg_proxy.py`` / the model / the fee config
would silently move the published headline with no audit trail.

This test pins the committed values. When it fails, that is the signal that a code
change altered the published numbers: re-run ``wck export-bundles`` and COMMIT the new
manifest deliberately (the provenance block records which code produced it). It is a
tripwire against accidental drift, not a correctness assertion about the strategy.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST = REPO_ROOT / "web" / "public" / "bundles" / "manifest.json"

# The committed headline. Update DELIBERATELY (with a fresh export) when the model changes.
GOLDEN_AGGREGATE = {
    "n_matches": 36,
    "n_fills": 282,
    "avg_clv_preoff": -0.0817,
    "clv_n_preoff": 282,
    "avg_clv_5m": -0.0018,
    "avg_clv": 0.04,
    "roi": -0.0139,
    "realized_pnl": -1.39,
}
GOLDEN_CALIBRATION = {"ece": 0.0317, "brier": 0.4259}


@pytest.fixture(scope="module")
def aggregate() -> dict:
    if not MANIFEST.exists():
        pytest.skip(f"published manifest not present: {MANIFEST}")
    return json.loads(MANIFEST.read_text())["aggregate"]


@pytest.mark.parametrize("key,expected", GOLDEN_AGGREGATE.items())
def test_published_aggregate_unchanged(aggregate, key, expected):
    actual = aggregate.get(key)
    assert actual == pytest.approx(expected, abs=1e-4), (
        f"published manifest aggregate['{key}'] drifted: {actual} != {expected}. "
        "If a model/config change caused this, re-run `wck export-bundles` and commit "
        "the new manifest.json (provenance records which code produced it)."
    )


@pytest.mark.parametrize("key,expected", GOLDEN_CALIBRATION.items())
def test_published_calibration_unchanged(aggregate, key, expected):
    actual = aggregate.get("calibration", {}).get(key)
    assert actual == pytest.approx(expected, abs=1e-4), (
        f"published calibration['{key}'] drifted: {actual} != {expected}. "
        "Re-export and commit deliberately if a code change caused this."
    )
