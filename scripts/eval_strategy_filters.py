#!/usr/bin/env python3
"""Pre-registered, held-out, multiplicity-corrected evaluation of the strategy filters (A1).

The web sandbox lets you drag sell-only / max-entry against the same 36 bundles until the
pooled CLV looks better — which is exactly the in-sample, threshold-mined, sample-halving
trap that manufactures fake edge. This script does the honest version:

  * PRE-REGISTERED hypotheses — a fixed, small set justified by MECHANISM, not by the data
    (sell-only: backs pay the spread on the expensive side and the detector charges taker
    fee on a fully-filling cross-spread, so backing is structurally the more −EV side;
    late entries bleed as in-play variance collapses near settlement).
  * HOLDOUT — split matches into train / test; pick the single best filter on TRAIN ONLY,
    then report its TEST CLV (an unbiased estimate).
  * MULTIPLICITY — report ALL hypotheses' test CLV (not just the winner), and use a
    Bonferroni-adjusted interval for the winner-vs-baseline paired test.

Honest ceiling: the best a filter can do is make the bleed LESS BAD — it removes −EV trades,
it does not manufacture +EV. A filter "winning" on test that still has a negative CLV is the
expected result, not a discovery.

Usage:  python scripts/eval_strategy_filters.py [--bundles web/public/bundles] [--seed 0] [--test-frac 0.5]
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from wc_kalshi.backtest.replay import bootstrap_ci, clustered_clv_ci  # noqa: E402

# PRE-REGISTERED hypothesis set — fixed BEFORE looking at the data. (action, minute) -> keep.
FILTERS = {
    "baseline": lambda a, m: True,
    "sell_only": lambda a, m: a == "sell",
    "max_entry_60": lambda a, m: m <= 60,
    "max_entry_70": lambda a, m: m <= 70,
    "max_entry_80": lambda a, m: m <= 80,
    "sell_only+max70": lambda a, m: a == "sell" and m <= 70,
}
CANDIDATES = [k for k in FILTERS if k != "baseline"]


def _signed_clv(action: str, entry: float, ref: float) -> float:
    return (ref - entry) if action == "buy" else (entry - ref)


def _match_fills(bundle: dict) -> list[tuple[str, int, float]]:
    """[(action, minute, preoff_clv)] for a settled bundle's golden fills."""
    preoff = bundle.get("preoff", {})
    out: list[tuple[str, int, float]] = []
    for f in bundle.get("golden", {}).get("fills", []):
        ref = preoff.get(f["outcome"])
        if ref is None:
            continue
        out.append((f["action"], f["minute"], _signed_clv(f["action"], f["entry_cents"] / 100.0, ref)))
    return out


def _by_match(matches: list, filt) -> dict[str, list[float]]:
    out: dict[str, list[float]] = {}
    for mid, fills in matches:
        kept = [clv for (a, m, clv) in fills if filt(a, m)]
        if kept:
            out[mid] = kept
    return out


def _pooled(by_match: dict[str, list[float]]) -> tuple[float, int]:
    vals = [c for lst in by_match.values() for c in lst]
    return (sum(vals) / len(vals) if vals else 0.0), len(vals)


def main() -> int:
    ap = argparse.ArgumentParser(description="pre-registered held-out eval of strategy filters")
    ap.add_argument("--bundles", default="web/public/bundles")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--test-frac", type=float, default=0.5)
    args = ap.parse_args()

    bdir = (REPO_ROOT / args.bundles) if not Path(args.bundles).is_absolute() else Path(args.bundles)
    manifest = json.loads((bdir / "manifest.json").read_text())
    matches = []
    for m in manifest["matches"]:
        fp = bdir / f"{m['match_id']}.json"
        if not fp.exists():
            continue
        fills = _match_fills(json.loads(fp.read_text()))
        if fills:
            matches.append((m["match_id"], fills))
    if len(matches) < 6:
        print(f"too few usable matches ({len(matches)})")
        return 1

    rng = random.Random(args.seed)
    order = matches[:]
    rng.shuffle(order)
    n_test = max(2, round(args.test_frac * len(order)))
    test, train = order[:n_test], order[n_test:]

    k = len(CANDIDATES)
    alpha = 0.05 / k  # Bonferroni over the k pre-registered candidates

    train_clv = {name: _pooled(_by_match(train, FILTERS[name]))[0] for name in FILTERS}
    best = max(CANDIDATES, key=lambda name: train_clv[name])  # least-negative on TRAIN

    print("=" * 76)
    print("STRATEGY-FILTER EVAL — pre-registered, held-out, multiplicity-corrected")
    print("=" * 76)
    print(f"  matches: {len(matches)}   train: {len(train)}   test: {len(test)}   seed: {args.seed}")
    print(f"  pre-registered candidates (k={k}): {CANDIDATES}")
    print(f"  Bonferroni alpha = 0.05/{k} = {alpha:.4f}   (winner chosen on TRAIN only)")
    print(f"  winner on TRAIN: {best}  (train CLV {train_clv[best]:+.4f} vs baseline {train_clv['baseline']:+.4f})")
    print("-" * 76)
    print(f"  {'filter':<18}{'TEST CLV':>10}{'fills':>7}{'matches':>9}   {'95% CI (Bonferroni)':>26}")
    for name in FILTERS:
        bm = _by_match(test, FILTERS[name])
        clv, n = _pooled(bm)
        lo, hi, nc = clustered_clv_ci(bm, alpha=alpha)
        mark = "  <- winner" if name == best else ("  (baseline)" if name == "baseline" else "")
        print(f"  {name:<18}{clv:>+10.4f}{n:>7}{nc:>9}   [{lo:+.4f}, {hi:+.4f}]{mark}")
    print("-" * 76)

    # Paired winner-vs-baseline on TEST: per-match (winner_clv − baseline_clv), Bonferroni CI.
    diffs = []
    for _mid, fills in test:
        base = [clv for (a, m, clv) in fills]
        filt = [clv for (a, m, clv) in fills if FILTERS[best](a, m)]
        if base and filt:
            diffs.append(sum(filt) / len(filt) - sum(base) / len(base))
    best_test_clv = _pooled(_by_match(test, FILTERS[best]))[0]
    if diffs:
        md = sum(diffs) / len(diffs)
        lo, hi = bootstrap_ci(diffs, alpha=alpha)
        if lo > 0:
            verdict = f"{best} SIGNIFICANTLY improves CLV vs baseline OOS (Bonferroni CI > 0)"
        elif hi < 0:
            verdict = f"{best} is significantly WORSE than baseline OOS"
        else:
            verdict = "NO demonstrated improvement over baseline (Bonferroni CI brackets 0)"
        print(f"  paired winner−baseline (TEST): {md:+.4f}  {100*(1-alpha):.0f}% CI [{lo:+.4f}, {hi:+.4f}] over {len(diffs)} matches")
        print(f"  VERDICT: {verdict}")
    ceiling = "still NEGATIVE (filters remove −EV trades; they do not create +EV)" if best_test_clv < 0 else "non-negative"
    print(f"  winner TEST CLV {best_test_clv:+.4f} → {ceiling}")
    print("  (train/test halves an already-tiny n and filters shrink fills further — wide CIs")
    print("   and a null result are the expected, honest outcome here.)")
    print("=" * 76)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
