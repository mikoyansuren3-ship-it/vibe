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

# The committed headline — the reproducible Kelly baseline from wc_tournament.sqlite3 at
# --bankroll 100 (the web convention). provenance is stamped in manifest["provenance"].
# Update DELIBERATELY, with a fresh `wck export-bundles --bankroll 100` + re-commit, when
# the model/config/DB changes. NOTE: Kelly fills are bankroll-sensitive via the position/
# exposure caps, so the bankroll is part of the reproducibility contract.
GOLDEN_AGGREGATE = {
    "n_matches": 36,
    "n_fills": 282,
    "stake_mode": "kelly",
    "avg_clv_preoff": -0.0817,
    "clv_n_preoff": 282,
    "avg_clv_5m": -0.0018,
    "avg_clv": 0.04,
    "roi": -0.0139,
    "realized_pnl": -1.39,
    "edge_verdict": "negative",
    "n_clusters_preoff": 35,
}
GOLDEN_CALIBRATION = {"ece": 0.0317, "brier": 0.4259}
GOLDEN_CLV_CI_PREOFF = [-0.1087, -0.0548]  # match-clustered 95% CI (deterministic, seed=0)
# Look-ahead-free fixed-stake cross-check (manifest["edge_eval"]).
GOLDEN_EDGE_EVAL = {"n_fills": 302, "stake_mode": "fixed", "avg_clv_preoff": -0.041, "edge_verdict": "negative"}


@pytest.fixture(scope="module")
def manifest() -> dict:
    if not MANIFEST.exists():
        pytest.skip(f"published manifest not present: {MANIFEST}")
    return json.loads(MANIFEST.read_text())


@pytest.fixture(scope="module")
def aggregate(manifest) -> dict:
    return manifest["aggregate"]


@pytest.mark.parametrize("key,expected", GOLDEN_AGGREGATE.items())
def test_published_aggregate_unchanged(aggregate, key, expected):
    actual = aggregate.get(key)
    match = actual == expected if isinstance(expected, str) else actual == pytest.approx(expected, abs=1e-4)
    assert match, (
        f"published manifest aggregate['{key}'] drifted: {actual!r} != {expected!r}. "
        "If a model/config change caused this, re-run `wck export-bundles --bankroll 100` "
        "and commit the new manifest.json (provenance records which code produced it)."
    )


@pytest.mark.parametrize("key,expected", GOLDEN_EDGE_EVAL.items())
def test_published_edge_eval_unchanged(manifest, key, expected):
    actual = manifest.get("edge_eval", {}).get(key)
    match = actual == expected if isinstance(expected, str) else actual == pytest.approx(expected, abs=1e-4)
    assert match, (
        f"published edge_eval['{key}'] (fixed-stake cross-check) drifted: {actual!r} != {expected!r}."
    )


def test_published_clv_ci_unchanged(aggregate):
    ci = aggregate.get("clv_ci_preoff")
    assert ci == pytest.approx(GOLDEN_CLV_CI_PREOFF, abs=1e-3), (
        f"match-clustered CLV CI drifted: {ci} != {GOLDEN_CLV_CI_PREOFF}. Re-export + re-commit if intended."
    )


@pytest.mark.parametrize("key,expected", GOLDEN_CALIBRATION.items())
def test_published_calibration_unchanged(aggregate, key, expected):
    actual = aggregate.get("calibration", {}).get(key)
    assert actual == pytest.approx(expected, abs=1e-4), (
        f"published calibration['{key}'] drifted: {actual} != {expected}. "
        "Re-export and commit deliberately if a code change caused this."
    )
