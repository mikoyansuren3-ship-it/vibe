#!/usr/bin/env python3
"""Honest, held-out evaluation of market-as-prior shrinkage (edge.market_pool_weight).

Question: does blending the model with the de-vigged market — p ∝ p_model**w · p_market**(1-w)
— FORECAST the result better than the market alone? If not, the model adds nothing and the
honest weight is w=0 (never deviate). The market is usually sharper, so expect a humbling
answer.

Method (no overfitting):
  * read the exported per-match bundles (model probs + market quotes per tick + final result);
  * split matches into TRAIN / TEST by a seeded shuffle;
  * pick w* on TRAIN only (min mean per-match multiclass log-loss);
  * report TEST log-loss for model-only (w=1), market-only (w=0), and the pool (w*);
  * the verdict is a PAIRED, MATCH-CLUSTERED bootstrap CI of (pool − market) per-match
    log-loss on TEST. If that interval brackets 0, the pool is NOT demonstrably better.

Usage:  python scripts/eval_market_pool.py [--bundles web/public/bundles] [--seed 0] [--test-frac 0.5]
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from wc_kalshi.backtest.replay import bootstrap_ci  # noqa: E402

_OUT_IDX = {"H": 0, "D": 1, "A": 2}


def _devig(mids: list[float]) -> list[float]:
    total = sum(mids)
    return [m / total for m in mids] if total > 0 else mids


def _pool(model: list[float], market: list[float], w: float) -> list[float]:
    vals = [max(model[i], 1e-9) ** w * max(market[i], 1e-9) ** (1.0 - w) for i in range(3)]
    z = sum(vals)
    return [v / z for v in vals] if z > 0 else model


def _match_rows(bundle: dict):
    """(model_prob_rows, market_prob_rows, outcome_idx) over ticks with a full two-sided
    1X2 book, or None if the match is unsettled / has no usable ticks."""
    outcome = bundle.get("outcome")
    if outcome not in _OUT_IDX:
        return None
    oi = _OUT_IDX[outcome]
    rows_model: list[list[float]] = []
    rows_market: list[list[float]] = []
    for t in bundle.get("ticks", []):
        mk = t.get("markets", {})
        if not all(o in mk and mk[o][0] is not None and mk[o][1] is not None for o in ("home", "draw", "away")):
            continue
        mids = [(mk[o][0] + mk[o][1]) / 200.0 for o in ("home", "draw", "away")]
        rows_model.append(t["model"])
        rows_market.append(_devig(mids))
    if not rows_model:
        return None
    return rows_model, rows_market, oi


def _per_match_ll(rows_model, rows_market, oi: int, w: float) -> float:
    """Mean multiclass log-loss of the pooled forecast over a match's ticks."""
    tot = 0.0
    for pm, pk in zip(rows_model, rows_market):
        p = _pool(pm, pk, w)
        tot += -math.log(max(p[oi], 1e-12))
    return tot / len(rows_model)


def main() -> int:
    ap = argparse.ArgumentParser(description="held-out eval of market_pool_weight")
    ap.add_argument("--bundles", default="web/public/bundles", help="dir with manifest.json + per-match bundles")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--test-frac", type=float, default=0.5)
    ap.add_argument("--grid", type=str, default="0,0.1,0.2,0.3,0.4,0.5,0.7,1.0")
    args = ap.parse_args()

    bdir = (REPO_ROOT / args.bundles) if not Path(args.bundles).is_absolute() else Path(args.bundles)
    manifest = json.loads((bdir / "manifest.json").read_text())
    matches = []
    for m in manifest["matches"]:
        fp = bdir / f"{m['match_id']}.json"
        if not fp.exists():
            continue
        parsed = _match_rows(json.loads(fp.read_text()))
        if parsed is not None:
            matches.append((m["match_id"], *parsed))
    if len(matches) < 6:
        print(f"too few usable matches ({len(matches)}) for a held-out split")
        return 1

    grid = [float(x) for x in args.grid.split(",")]
    rng = random.Random(args.seed)
    order = matches[:]
    rng.shuffle(order)
    n_test = max(2, int(round(args.test_frac * len(order))))
    test, train = order[:n_test], order[n_test:]

    def mean_ll(subset, w: float) -> float:
        vals = [_per_match_ll(rm, rk, oi, w) for _, rm, rk, oi in subset]
        return sum(vals) / len(vals)

    # pick w* on TRAIN only
    w_star = min(grid, key=lambda w: mean_ll(train, w))

    print("=" * 68)
    print("MARKET-POOL HELD-OUT EVAL  (lower log-loss = better forecast)")
    print("=" * 68)
    print(f"  usable matches: {len(matches)}   train: {len(train)}   test: {len(test)}   seed: {args.seed}")
    print(f"  w grid:         {grid}")
    print(f"  w* (train-picked, min train log-loss): {w_star}")
    print("-" * 68)
    print(f"  {'forecast':<22}{'TEST log-loss':>16}")
    for label, w in (("model only (w=1)", 1.0), ("market only (w=0)", 0.0), (f"pool (w*={w_star})", w_star)):
        print(f"  {label:<22}{mean_ll(test, w):>16.4f}")
    print("-" * 68)

    # PAIRED, match-clustered verdict: per-match (pool − market) log-loss on TEST.
    diffs = [_per_match_ll(rm, rk, oi, w_star) - _per_match_ll(rm, rk, oi, 0.0) for _, rm, rk, oi in test]
    mean_d = sum(diffs) / len(diffs)
    lo, hi = bootstrap_ci(diffs, iters=5000)
    if hi < 0:
        verdict = "POOL BEATS MARKET out-of-sample (CI below 0)"
    elif lo > 0:
        verdict = "pool is WORSE than market out-of-sample (CI above 0)"
    else:
        verdict = "NO demonstrated improvement over the market (CI brackets 0) → honest w = 0"
    print(f"  pool − market (test, per-match): {mean_d:+.4f}   95% CI [{lo:+.4f}, {hi:+.4f}]")
    print(f"  VERDICT: {verdict}")
    print("  (n is small — a null result here is the expected, honest outcome, not a bug.)")
    print("=" * 68)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
