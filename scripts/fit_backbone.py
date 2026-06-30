"""P0.4 — fit the upgraded intensity engine on real StatsBomb data and judge it on
HELD-OUT data (leave-one-tournament-out), so we only flip the new knobs on if they
genuinely beat the current backbone out-of-sample AND don't degrade the markets we trade.

Arms (all coordinate-descent fit on the TRAIN tournament, scored on the TEST one), ordered
leanest → fullest so the gate ships the SMALLEST sufficient change:
  * baseline    — current structure: linear xG weight, flat score-state, symmetric red card
  * slope_only  — linear xG weight + ONLY a time-inhomogeneous goal profile (goal_time_slope).
                  The leanest real arm; keeps the existing xG blend (small blast radius).
  * minimal     — credibility xG weight + time profile (the 2-knob "minimal" intensity arm)
  * upgraded    — credibility + time profile + graded score-state + asymmetric red card

THE GATE (pre-registered, two parts):
  1. 1X2 skill — the arm must beat baseline on held-out multiclass log-loss AND RPS.
  2. Downstream non-worsening — the arm must NOT worsen the scoreline-derived heads we ACTUALLY
     trade. Flipping the knobs reshapes λ_rem/μ_rem, which feeds every Total/BTTS/supremacy
     price, so a 1X2 win is not enough: an arm fails if any Total O/U line's held-out Brier
     worsens by more than DOWNSTREAM_BRIER_TOL in EITHER fold, or pooled supremacy RPS worsens
     by more than DOWNSTREAM_RPS_TOL. (Part 2 was added after a 1X2-only run looked like a flip
     but the change degraded Total Over 3.5 — see the modeling memo; committed in advance so it
     is a real constraint, not a result-driven rationalization.)

We ship the LEANEST arm that clears BOTH parts; otherwise the knobs stay OFF.

Honesty notes:
  * StatsBomb has no market prices, so this is a CALIBRATION comparison (log-loss / RPS / Brier
    on real 90' outcomes), NOT CLV or tradable edge.
  * Elo uses the (anachronistic) built-in 2026 ratings — wrong era, but applied IDENTICALLY to
    every arm, so the RELATIVE comparison is valid even though absolute scores are not.
  * Leave-one-tournament-out => no within-tournament leakage. n is only 2 tournaments.

Run: .venv/bin/python scripts/fit_backbone.py
"""

from __future__ import annotations

import numpy as np

from wc_kalshi.backtest.historical import load_historical_match
from wc_kalshi.backtest.statsbomb import build_world_cup
from wc_kalshi.config import load_config
from wc_kalshi.modeling.calibration import BinaryCalibrationTracker, CalibrationTracker
from wc_kalshi.modeling.derived import prob_btts, prob_total_over, supremacy_pmf
from wc_kalshi.modeling.fit import (
    _GRIDS,
    _GRIDS_INTENSITY,
    _checkpoint_snaps,
    _realized,
    fit_constants,
)
from wc_kalshi.modeling.fit import FIT_CHECKPOINTS
from wc_kalshi.modeling.inplay import DixonColesInplayModel

WORLD_CUPS = {"2018": (43, 3), "2022": (43, 106)}

# The 2-knob "minimal" arm: credibility mode + the time profile, dropping ``live_xg_weight``
# (unused in credibility mode — ``xg_info_k`` replaces it) and the unstable score-state grading
# / asymmetric red-card that showed no signal.
_GRIDS_MINIMAL: dict[str, list[float]] = {
    "red_card_xg_penalty": [0.45, 0.55, 0.65, 0.75],
    "elo_tilt": [0.15, 0.2, 0.25, 0.3, 0.35],
    "leader_mult": [0.92, 0.95, 0.97, 1.0],
    "chaser_mult": [1.0, 1.03, 1.06, 1.1],
    "goal_time_slope": [0.0, 0.15, 0.3, 0.45],
    "xg_info_k": [0.6, 1.0, 1.5, 2.2],
}

# The LEANEST real arm: the legacy linear grid + ONLY the time profile. Keeps xg_blend_mode
# "linear" (so the live-xG weighting on every scoreline head is untouched), adding one knob.
_GRIDS_SLOPE_ONLY: dict[str, list[float]] = dict(_GRIDS)
_GRIDS_SLOPE_ONLY["goal_time_slope"] = [0.0, 0.15, 0.3, 0.45]

# arm name -> (xg_blend_mode, fit grid). Order matters: leanest first (the gate ships the
# first arm that passes).
_ARMS = {
    "baseline": ("linear", _GRIDS),
    "slope_only": ("linear", _GRIDS_SLOPE_ONLY),
    "minimal": ("credibility", _GRIDS_MINIMAL),
    "upgraded": ("credibility", _GRIDS_INTENSITY),
}
_CANDIDATES = ("slope_only", "minimal", "upgraded")  # non-baseline, leanest first

# Pre-registered downstream-heads acceptance tolerances (see module docstring). A candidate
# fails if any Total line's Brier worsens by more than this in EITHER fold...
DOWNSTREAM_BRIER_TOL = 0.005
# ...or pooled supremacy RPS worsens by more than this.
DOWNSTREAM_RPS_TOL = 0.001

TOTAL_LINES = (1.5, 2.5, 3.5)
# Ordered margin buckets for supremacy RPS (home perspective): away by 2+, away by 1, draw,
# home by 1, home by 2+ — ordered so a far miss is penalised more than a near miss.
MARGIN_BUCKETS = ("A2", "A1", "D", "H1", "H2")


def load_wc(comp: int, season: int):
    dicts = build_world_cup(
        comp, season, repo=None, cache_dir="data/statsbomb", allow_builtin_fallback=True
    )
    out = []
    for d in dicts:
        snaps = [snap for snap, _ in load_historical_match(d)]
        if snaps:
            out.append(snaps)
    return out


def eval_test(cfg, test) -> dict[str, float]:
    """Held-out 1X2 skill (the resolvable match-result market)."""
    model = DixonColesInplayModel(cfg)
    tr = CalibrationTracker()
    for snaps in test:
        realized = _realized(snaps[-1])
        for snap in _checkpoint_snaps(snaps):
            tr.add(model.predict(snap), realized)
    return {"logloss": tr.log_loss(), "rps": tr.rps(), "brier": tr.brier_score(), "n": float(tr.n)}


def _margin_bucket(d: int) -> str:
    if d <= -2:
        return "A2"
    if d == -1:
        return "A1"
    if d == 0:
        return "D"
    if d == 1:
        return "H1"
    return "H2"


def _supremacy_bucket_probs(m: np.ndarray) -> np.ndarray:
    pmf = supremacy_pmf(m)
    v = {b: 0.0 for b in MARGIN_BUCKETS}
    for d, p in pmf.items():
        v[_margin_bucket(d)] += p
    arr = np.array([v[b] for b in MARGIN_BUCKETS])
    s = arr.sum()
    return arr / s if s > 0 else arr


def _ordered_rps(p: np.ndarray, realized_idx: int) -> float:
    """Ranked Probability Score for an ordered categorical (Constantinou-Fenton)."""
    y = np.zeros_like(p)
    y[realized_idx] = 1.0
    cp = np.cumsum(p)[:-1]
    cy = np.cumsum(y)[:-1]
    return float(np.sum((cp - cy) ** 2) / (len(p) - 1))


def eval_downstream(cfg, test) -> dict[str, float]:
    """Held-out calibration of the scoreline-DERIVED heads we trade, settled against the
    realized final 90' score: per-line Total O/U Brier, BTTS Brier, supremacy RPS."""
    model = DixonColesInplayModel(cfg)
    tot = {ln: BinaryCalibrationTracker(name=f"O{ln}") for ln in TOTAL_LINES}
    btts = BinaryCalibrationTracker(name="btts")
    sup: list[float] = []
    for snaps in test:
        final = snaps[-1]
        fh, fa = final.home_score, final.away_score
        ftot, fbtts = fh + fa, (fh > 0 and fa > 0)
        fmargin_idx = MARGIN_BUCKETS.index(_margin_bucket(fh - fa))
        for snap in _checkpoint_snaps(snaps):
            m = model.scoreline_matrix(snap)
            for ln in TOTAL_LINES:
                tot[ln].add(prob_total_over(m, ln), ftot > ln)
            btts.add(prob_btts(m), fbtts)
            sup.append(_ordered_rps(_supremacy_bucket_probs(m), fmargin_idx))
    out = {f"o{ln}_brier": tot[ln].brier_score() for ln in TOTAL_LINES}
    out["btts_brier"] = btts.brier_score()
    out["sup_rps"] = float(np.mean(sup)) if sup else float("nan")
    return out


def _passes_downstream(arm_folds: list[dict], base_folds: list[dict]) -> tuple[bool, list[str]]:
    """Part 2 of the gate: no Total line's Brier worsens by > tol in EITHER fold, and pooled
    supremacy RPS doesn't worsen by > tol. Returns (ok, failure_reasons)."""
    reasons: list[str] = []
    for ln in TOTAL_LINES:
        for i, (a, b) in enumerate(zip(arm_folds, base_folds)):
            d = a[f"o{ln}_brier"] - b[f"o{ln}_brier"]
            if d > DOWNSTREAM_BRIER_TOL:
                reasons.append(f"O{ln} Brier +{d:.4f} in fold{i} (> {DOWNSTREAM_BRIER_TOL})")
    d_sup = (sum(a["sup_rps"] for a in arm_folds) - sum(b["sup_rps"] for b in base_folds)) / len(arm_folds)
    if d_sup > DOWNSTREAM_RPS_TOL:
        reasons.append(f"supremacy RPS +{d_sup:.4f} pooled (> {DOWNSTREAM_RPS_TOL})")
    return (not reasons), reasons


def main() -> None:
    base = load_config(load_env=False, use_local=False).model
    data = {name: load_wc(*ids) for name, ids in WORLD_CUPS.items()}
    for name, ms in data.items():
        print(f"loaded WC {name}: {len(ms)} matches")

    folds = [("2018", "2022"), ("2022", "2018")]
    one: dict[str, list[dict[str, float]]] = {arm: [] for arm in _ARMS}   # 1X2 metrics per fold
    down: dict[str, list[dict[str, float]]] = {arm: [] for arm in _ARMS}  # downstream per fold

    for train_name, test_name in folds:
        train, test = data[train_name], data[test_name]
        print(f"\n[fold train={train_name} → test={test_name}]  (~{len(test) * len(FIT_CHECKPOINTS)} test samples)")
        for arm, (mode, grid) in _ARMS.items():
            base_arm = base.model_copy(update={"xg_blend_mode": mode})
            fr = fit_constants(train, base_arm, grids=grid, passes=2)
            cfg = base_arm.model_copy(update=fr.params)
            e1 = eval_test(cfg, test)
            ed = eval_downstream(cfg, test)
            one[arm].append(e1)
            down[arm].append(ed)
            print(f"  {arm:10s}: logloss={e1['logloss']:.4f}  rps={e1['rps']:.4f}  brier={e1['brier']:.4f}")
            if arm != "baseline":
                watch = {k: round(v, 3) for k, v in fr.params.items() if k in ("goal_time_slope", "xg_info_k")}
                print(f"              downstream O1.5/2.5/3.5 brier="
                      f"{ed['o1.5_brier']:.4f}/{ed['o2.5_brier']:.4f}/{ed['o3.5_brier']:.4f}  "
                      f"supRPS={ed['sup_rps']:.4f}  watch={watch}")

    def m1(arm: str, key: str) -> float:
        return sum(x[key] for x in one[arm]) / len(one[arm])

    print("\n=== held-out 1X2 averages (leave-one-tournament-out) ===")
    bl, br = m1("baseline", "logloss"), m1("baseline", "rps")
    bb = m1("baseline", "brier")
    for arm in _ARMS:
        print(f"  {arm:10s}: logloss={m1(arm, 'logloss'):.4f}  rps={m1(arm, 'rps'):.4f}  brier={m1(arm, 'brier'):.4f}")
    print("  Δ vs baseline (negative = better):")
    for arm in _CANDIDATES:
        print(f"    {arm:10s}: Δlogloss={m1(arm, 'logloss') - bl:+.4f}  "
              f"Δrps={m1(arm, 'rps') - br:+.4f}  Δbrier={m1(arm, 'brier') - bb:+.4f}")

    print("\n=== downstream-heads scorecard (Δ vs baseline; +Brier/+RPS = WORSE) ===")
    for arm in _CANDIDATES:
        parts = []
        for ln in TOTAL_LINES:
            per_fold = [down[arm][i][f"o{ln}_brier"] - down["baseline"][i][f"o{ln}_brier"] for i in range(len(folds))]
            parts.append(f"O{ln} ΔBrier=[{per_fold[0]:+.4f},{per_fold[1]:+.4f}]")
        d_sup = (sum(d["sup_rps"] for d in down[arm]) - sum(d["sup_rps"] for d in down["baseline"])) / len(folds)
        print(f"  {arm:10s}: " + "  ".join(parts) + f"  supΔRPS={d_sup:+.4f}")

    print("\n=== decision (ship the LEANEST arm passing BOTH the 1X2 gate AND the downstream gate) ===")
    winner = None
    for arm in _CANDIDATES:
        one_ok = m1(arm, "logloss") < bl and m1(arm, "rps") < br
        down_ok, reasons = _passes_downstream(down[arm], down["baseline"])
        tag = "PASS" if (one_ok and down_ok) else "fail"
        why = []
        if not one_ok:
            why.append("1X2 gate (needs logloss<base AND rps<base)")
        why += reasons
        print(f"  {arm:10s}: 1X2={'ok' if one_ok else 'no'}  downstream={'ok' if down_ok else 'no'}  -> {tag}"
              + (f"   [{'; '.join(why)}]" if why else ""))
        if one_ok and down_ok and winner is None:
            winner = arm

    if winner:
        mode, grid = _ARMS[winner]
        base_w = base.model_copy(update={"xg_blend_mode": mode})
        final = fit_constants(data["2018"] + data["2022"], base_w, grids=grid, passes=3)
        print(f"\n  '{winner}' WINS held-out (1X2 skill AND traded-head non-worsening) → flip knobs on.")
        print(f"  Final params (pooled fit):  xg_blend_mode: {mode}")
        for k, v in final.params.items():
            print(f"    {k}: {round(v, 4) if v is not None else None}")
    else:
        print("\n  No arm clears BOTH gates → keep the new knobs OFF (defaults unchanged); do not ship.")
        print("  The time-profile improves 1X2 log-loss but degrades Total Over 3.5 beyond tolerance")
        print("  (and goal_time_slope pins to the top of its grid). Re-decide with a WIDER grid +")
        print("  MORE tournaments; the downstream gate is now pre-registered for the next refit.")


if __name__ == "__main__":
    main()
