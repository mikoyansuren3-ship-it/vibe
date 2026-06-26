#!/usr/bin/env python3
"""Fit the shot-based xG proxy weights on StatsBomb open data.

Regresses cumulative real per-shot xG on cumulative shot counts (no intercept, so
0 shots => 0 xG):

    xG ≈ w_sot·(shots on target) + w_off·(off-target / blocked shots)

Prints the per-tick fit (every shot event = one cumulative sample) and an
independent per-match-endpoint fit as a cross-check. Copy the coefficients into
``modeling/xg_proxy.py`` (DEFAULT_W_SOT / DEFAULT_W_OFF) and ``config.ModelSection``.

Usage:
    python scripts/fit_xg_proxy.py [--events-dir data/statsbomb/events]

StatsBomb open data is CC BY-NC-SA (attribution required); it is git-ignored here.
"""

from __future__ import annotations

import argparse
import glob
import json

import numpy as np

# StatsBomb shot outcomes that count as "on target" (force a save or score) — the
# same notion as API-Football's "Shots on Goal".
ON_TARGET = frozenset({"Goal", "Saved", "Saved to Post"})
REGULATION_PERIODS = frozenset({1, 2})


def _fit(rows: list[tuple[int, int, float]], label: str) -> np.ndarray:
    a = np.array([[r[0], r[1]] for r in rows], dtype=float)  # [sot, off]
    y = np.array([r[2] for r in rows], dtype=float)
    coef, *_ = np.linalg.lstsq(a, y, rcond=None)
    pred = a @ coef
    ss_res = float(((y - pred) ** 2).sum())
    ss_tot = float(((y - y.mean()) ** 2).sum())
    r2 = 1 - ss_res / ss_tot if ss_tot else float("nan")
    print(f"{label}: n={len(rows)}  w_sot={coef[0]:.4f}  w_off={coef[1]:.4f}  R2={r2:.4f}")
    return coef


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--events-dir", default="data/statsbomb/events")
    args = ap.parse_args()

    tick_rows: list[tuple[int, int, float]] = []  # cumulative sample at each shot
    endpoint_rows: list[tuple[int, int, float]] = []  # final state per team per match
    n_matches = 0

    for fp in sorted(glob.glob(f"{args.events_dir}/*.json")):
        events = json.load(open(fp))
        shots = sorted(
            (e for e in events
             if e.get("period") in REGULATION_PERIODS and e["type"]["name"] == "Shot"),
            key=lambda e: (e["period"], e.get("minute", 0), e.get("second", 0), e.get("index", 0)),
        )
        if not any(e.get("shot", {}).get("statsbomb_xg") is not None for e in shots):
            continue  # older seasons without xG
        n_matches += 1
        cum: dict[str, list] = {}  # team -> [sot, off, xg]
        for e in shots:
            xg = e.get("shot", {}).get("statsbomb_xg")
            if xg is None:
                continue
            c = cum.setdefault(e.get("team", {}).get("name"), [0, 0, 0.0])
            if e["shot"].get("outcome", {}).get("name") in ON_TARGET:
                c[0] += 1
            else:
                c[1] += 1
            c[2] += float(xg)
            tick_rows.append((c[0], c[1], c[2]))
        for c in cum.values():
            endpoint_rows.append((c[0], c[1], c[2]))

    print(f"matches with xG: {n_matches}")
    _fit(tick_rows, "per-tick (cumulative at each shot)")
    _fit(endpoint_rows, "per-match endpoints (independent)")


if __name__ == "__main__":
    main()
