"""P0.4 — fit the upgraded intensity engine on real StatsBomb data and judge it on
HELD-OUT data (leave-one-tournament-out), so we only flip the new knobs on if they
genuinely beat the current backbone out-of-sample.

Two arms, both coordinate-descent fit on the TRAIN tournament, scored on the TEST one:
  * baseline  — current structure: linear xG weight, flat score-state, symmetric red card
  * upgraded  — credibility xG weight + time-profile + graded score-state + asymmetric red card

Honesty notes:
  * StatsBomb has no market prices, so this is a CALIBRATION comparison (log-loss / RPS /
    Brier on real 90' outcomes), NOT CLV or tradable edge.
  * Elo uses the (anachronistic) built-in 2026 ratings — wrong era, but applied IDENTICALLY
    to both arms, so the RELATIVE comparison is valid even though absolute log-loss is not.
  * Leave-one-tournament-out => no within-tournament leakage.

Run: .venv/bin/python scripts/fit_backbone.py
"""

from __future__ import annotations

from wc_kalshi.backtest.historical import load_historical_match
from wc_kalshi.backtest.statsbomb import build_world_cup
from wc_kalshi.config import load_config
from wc_kalshi.modeling.calibration import CalibrationTracker
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

# A leaner "minimal" arm: credibility mode with ONLY the two knobs that were consistently
# selected by the full fit (time-profile + xG credibility), dropping the unstable
# score-state grading and asymmetric red-card — fewer params, less overfitting.
_GRIDS_MINIMAL: dict[str, list[float]] = {
    "red_card_xg_penalty": [0.45, 0.55, 0.65, 0.75],
    "elo_tilt": [0.15, 0.2, 0.25, 0.3, 0.35],
    "leader_mult": [0.92, 0.95, 0.97, 1.0],
    "chaser_mult": [1.0, 1.03, 1.06, 1.1],
    "goal_time_slope": [0.0, 0.15, 0.3, 0.45],
    "xg_info_k": [0.6, 1.0, 1.5, 2.2],
}

# arm name -> (xg_blend_mode, fit grid)
_ARMS = {
    "baseline": ("linear", _GRIDS),
    "minimal": ("credibility", _GRIDS_MINIMAL),
    "upgraded": ("credibility", _GRIDS_INTENSITY),
}


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
    model = DixonColesInplayModel(cfg)
    tr = CalibrationTracker()
    for snaps in test:
        realized = _realized(snaps[-1])
        for snap in _checkpoint_snaps(snaps):
            tr.add(model.predict(snap), realized)
    return {"logloss": tr.log_loss(), "rps": tr.rps(), "brier": tr.brier_score(), "n": float(tr.n)}


def main() -> None:
    base = load_config(load_env=False, use_local=False).model
    data = {name: load_wc(*ids) for name, ids in WORLD_CUPS.items()}
    for name, ms in data.items():
        print(f"loaded WC {name}: {len(ms)} matches")

    folds = [("2018", "2022"), ("2022", "2018")]
    agg: dict[str, list[dict[str, float]]] = {arm: [] for arm in _ARMS}

    for train_name, test_name in folds:
        train, test = data[train_name], data[test_name]
        print(f"\n[fold train={train_name} → test={test_name}]  (~{len(test) * len(FIT_CHECKPOINTS)} test samples)")
        for arm, (mode, grid) in _ARMS.items():
            base_arm = base.model_copy(update={"xg_blend_mode": mode})
            fr = fit_constants(train, base_arm, grids=grid, passes=2)
            e = eval_test(base_arm.model_copy(update=fr.params), test)
            agg[arm].append(e)
            print(f"  {arm:9s}: logloss={e['logloss']:.4f}  rps={e['rps']:.4f}  brier={e['brier']:.4f}")
            if arm != "baseline":
                shown = {k: (round(v, 3) if v is not None else None) for k, v in fr.params.items()}
                print(f"             params: {shown}")

    def mean(arm: str, key: str) -> float:
        return sum(x[key] for x in agg[arm]) / len(agg[arm])

    print("\n=== held-out averages (leave-one-tournament-out) ===")
    for arm in _ARMS:
        print(f"  {arm:9s}: logloss={mean(arm, 'logloss'):.4f}  rps={mean(arm, 'rps'):.4f}  brier={mean(arm, 'brier'):.4f}")
    bl, br, bb = mean("baseline", "logloss"), mean("baseline", "rps"), mean("baseline", "brier")
    print("  Δ vs baseline (negative = better):")
    for arm in ("minimal", "upgraded"):
        print(f"    {arm:9s}: Δlogloss={mean(arm, 'logloss') - bl:+.4f}  Δrps={mean(arm, 'rps') - br:+.4f}  Δbrier={mean(arm, 'brier') - bb:+.4f}")

    print("\n=== decision (ship the LEANEST arm that beats baseline on log-loss AND RPS) ===")
    winner = None
    for arm in ("minimal", "upgraded"):  # leanest first
        if mean(arm, "logloss") < bl and mean(arm, "rps") < br:
            winner = arm
            break
    if winner:
        mode, grid = _ARMS[winner]
        base_w = base.model_copy(update={"xg_blend_mode": mode})
        final = fit_constants(data["2018"] + data["2022"], base_w, grids=grid, passes=3)
        print(f"  '{winner}' WINS held-out (log-loss AND RPS) → flip knobs on. Final params (pooled fit):")
        print(f"    xg_blend_mode: {mode}")
        for k, v in final.params.items():
            print(f"    {k}: {round(v, 4) if v is not None else None}")
    else:
        print("  No arm beats baseline on BOTH held-out log-loss AND RPS.")
        print("  → keep the new knobs OFF (defaults unchanged); do not ship. Consistent signal")
        print("    (time-profile + xG credibility improve log-loss) noted for a future fit with more data.")


if __name__ == "__main__":
    main()
