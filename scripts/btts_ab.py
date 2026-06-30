"""BTTS dependence A/B (plan Phase 1 "BTTS negative-correlation") — held-out, with a
significance floor. Extends the P0.4 backbone harness to the both-teams-to-score market.

THE QUESTION. Production prices BTTS as ``prob_btts = M[1:,1:].sum()`` over the joint final-score
matrix, whose only home/away dependence is the Dixon-Coles low-score ``draw_rho`` (fixed -0.05,
zeroed after any goal). The plan hypothesised that home/away goals are negatively correlated
(claimed r≈-0.085) so the model over-predicts BTTS, and proposed two fixes: keep the DC correction
active all match + re-fit ``draw_rho`` ("rho_persist"), or replace independent-Poisson+DC with a
Frank copula of one fitted ``theta`` ("frank", theta<0 = negative dependence).

THE VERDICT (this harness, StatsBomb WC2018+2022, leave-one-tournament-out): KEEP OFF.
  * Premise is weak: measured r(home,away)=-0.030 (not -0.085), SE≈0.09 at n=128 → indistinguishable
    from 0. The model does over-predict BTTS by ~3.6pp, but a dependence mechanism can't grab a
    correlation that isn't there.
  * Neither arm clears the SIGNIFICANCE FLOOR: the per-match-clustered bootstrap 95% CI for the
    held-out ΔBTTS-Brier CROSSES ZERO for both (rho_persist [-0.00077,+0.00029]; frank
    [-0.00181,+0.00109]). rho_persist only "improves" by fitting draw_rho=+0.05 — the OPPOSITE sign
    to the hypothesis — i.e. absorbing residual miscalibration, not modelling negative dependence.

THE GATE. A 1X2-only or point-estimate gate would have shipped a -0.0002 "gain" that is pure noise
(the earlier draft of this harness printed "SHIP" on exactly that). So an arm ships ONLY if the
match-clustered bootstrap CI for ΔBTTS-Brier EXCLUDES zero AND it does not worsen 1X2 / Total O/U.

Honesty: calibration on real 90' outcomes only (no Kalshi prices → no CLV/edge claim); n=2
tournaments; anachronistic 2026 Elo applied identically to every arm (relative valid, absolute not).

Run: .venv/bin/python scripts/btts_ab.py
"""
from __future__ import annotations

import math
import random

import numpy as np

from wc_kalshi.backtest.historical import load_historical_match
from wc_kalshi.backtest.statsbomb import build_world_cup
from wc_kalshi.config import load_config
from wc_kalshi.modeling.calibration import BinaryCalibrationTracker
from wc_kalshi.modeling.derived import prob_btts, prob_total_over, supremacy_pmf
from wc_kalshi.modeling.fit import _checkpoint_snaps
from wc_kalshi.modeling.inplay import DixonColesInplayModel
from wc_kalshi.modeling.poisson import poisson_pmf

WORLD_CUPS = {"2018": (43, 3), "2022": (43, 106)}
TOTAL_LINES = (1.5, 2.5, 3.5)
MARGIN_BUCKETS = ("A2", "A1", "D", "H1", "H2")
DRAW_RHO_GRID = [-0.12, -0.08, -0.05, -0.02, 0.0, 0.05]
THETA_GRID = [-2.5, -1.5, -1.0, -0.5, -0.25, 0.0]
# Non-worsening tolerances for the markets that share the joint matrix (mirrors P0.4).
X12_LL_TOL, X12_RPS_TOL, TOTAL_BRIER_TOL = 0.003, 0.001, 0.005
BOOT_RESAMPLES = 4000


def load_wc(comp: int, season: int):
    dicts = build_world_cup(comp, season, repo=None, cache_dir="data/statsbomb", allow_builtin_fallback=True)
    return [s for d in dicts if (s := [snap for snap, _ in load_historical_match(d)])]


# --- Frank copula joint remaining-goal matrix (negative theta = negative dependence) ------- #
def _poisson_cdf(lam: float, kmax: int) -> np.ndarray:
    return np.cumsum([poisson_pmf(lam, k) for k in range(kmax + 1)])


def _frank_C(u: np.ndarray, v: np.ndarray, theta: float) -> np.ndarray:
    if abs(theta) < 1e-9:
        return u * v
    return -1.0 / theta * np.log1p(np.expm1(-theta * u) * np.expm1(-theta * v) / math.expm1(-theta))


def frank_remaining_matrix(lam: float, mu: float, theta: float, max_goals: int) -> np.ndarray:
    """Joint P(remaining_home=i, remaining_away=j): Poisson marginals coupled by a Frank copula of
    dependence ``theta`` (rectangle difference of the copula CDF over the marginal-CDF grid, so the
    marginals stay Poisson; renormalised for the truncation tail)."""
    fh = np.concatenate([[0.0], _poisson_cdf(lam, max_goals)])  # fh[k+1]=F_h(k), fh[0]=0
    fa = np.concatenate([[0.0], _poisson_cdf(mu, max_goals)])
    UO, VO = np.meshgrid(fh[1:], fa[1:], indexing="ij")  # F(i),   F(j)
    UL, VL = np.meshgrid(fh[:-1], fa[:-1], indexing="ij")  # F(i-1), F(j-1)
    M = np.clip(_frank_C(UO, VO, theta) - _frank_C(UL, VO, theta)
                - _frank_C(UO, VL, theta) + _frank_C(UL, VL, theta), 0.0, None)
    s = M.sum()
    return M / s if s > 0 else M


# --- arm matrix-builders (differ ONLY in the dependence mechanism) ------------------------- #
class RhoPersistModel(DixonColesInplayModel):
    """DC low-score correction kept active the whole match (not zeroed after a goal)."""

    def _effective_rho(self, match) -> float:
        elapsed = min(max(match.minute, 0), 90)
        return self.cfg.draw_rho * max(0.0, 90.0 - elapsed) / 90.0


class FrankModel(DixonColesInplayModel):
    """Remaining-goal matrix from a Frank copula (theta) instead of independent Poisson + DC tau."""

    theta: float = 0.0

    def scoreline_matrix(self, match) -> np.ndarray:
        hs, as_ = match.home_score, match.away_score
        if match.period.is_finished or match.status == "finished":
            m = np.zeros((hs + 1, as_ + 1))
            m[hs, as_] = 1.0
            return m
        lam, mu = self._remaining_rates(match)
        rem = frank_remaining_matrix(lam, mu, self.theta, self.cfg.max_goals)
        n = rem.shape[0]
        m = np.zeros((n + hs, n + as_))
        m[hs:hs + n, as_:as_ + n] = rem
        return m


def builder_baseline(cfg):
    return DixonColesInplayModel(cfg).scoreline_matrix


def builder_rho_persist(cfg, draw_rho):
    return RhoPersistModel(cfg.model_copy(update={"draw_rho": draw_rho})).scoreline_matrix


def builder_frank(cfg, theta):
    m = FrankModel(cfg)
    m.theta = theta
    return m.scoreline_matrix


# --- metrics ------------------------------------------------------------------------------- #
def _margin_bucket(d: int) -> str:
    return "A2" if d <= -2 else "A1" if d == -1 else "D" if d == 0 else "H1" if d == 1 else "H2"


def _sup_bucket_probs(M: np.ndarray) -> np.ndarray:
    v = {b: 0.0 for b in MARGIN_BUCKETS}
    for d, p in supremacy_pmf(M).items():
        v[_margin_bucket(d)] += p
    arr = np.array([v[b] for b in MARGIN_BUCKETS])
    s = arr.sum()
    return arr / s if s > 0 else arr


def _ordered_rps(p: np.ndarray, idx: int) -> float:
    y = np.zeros_like(p)
    y[idx] = 1.0
    cp, cy = np.cumsum(p)[:-1], np.cumsum(y)[:-1]
    return float(np.sum((cp - cy) ** 2) / (len(p) - 1))


def _collapse_1x2(M: np.ndarray) -> tuple[float, float, float]:
    h = d = a = 0.0
    for i in range(M.shape[0]):
        for j in range(M.shape[1]):
            v = float(M[i, j])
            h, d, a = (h + v, d, a) if i > j else (h, d + v, a) if i == j else (h, d, a + v)
    return h, d, a


def evaluate(build_M, matches) -> dict[str, float]:
    btts = BinaryCalibrationTracker(name="btts")
    tot = {ln: BinaryCalibrationTracker(name=f"O{ln}") for ln in TOTAL_LINES}
    sup, x12_ll, x12_rps = [], [], []
    for snaps in matches:
        f = snaps[-1]
        ftot, fbtts, d = f.home_score + f.away_score, (f.home_score > 0 and f.away_score > 0), f.home_score - f.away_score
        out_idx = 2 if d > 0 else 1 if d == 0 else 0
        sup_idx = MARGIN_BUCKETS.index(_margin_bucket(d))
        for snap in _checkpoint_snaps(snaps):
            M = build_M(snap)
            btts.add(prob_btts(M), fbtts)
            for ln in TOTAL_LINES:
                tot[ln].add(prob_total_over(M, ln), ftot > ln)
            h, dr, aw = _collapse_1x2(M)
            pv = np.array([aw, dr, h])
            pv = pv / pv.sum()
            x12_ll.append(-math.log(max(1e-12, pv[out_idx])))
            x12_rps.append(_ordered_rps(pv, out_idx))
            sup.append(_ordered_rps(_sup_bucket_probs(M), sup_idx))
    out = {"btts_brier": btts.brier_score(), "btts_ll": btts.log_loss(),
           "x12_ll": float(np.mean(x12_ll)), "x12_rps": float(np.mean(x12_rps)), "sup_rps": float(np.mean(sup))}
    out.update({f"o{ln}_brier": tot[ln].brier_score() for ln in TOTAL_LINES})
    out.update({f"o{ln}_ll": tot[ln].log_loss() for ln in TOTAL_LINES})
    return out


def per_match_btts_brier(build_M, snaps) -> float:
    f = snaps[-1]
    fbtts = 1.0 if (f.home_score > 0 and f.away_score > 0) else 0.0
    return float(np.mean([(prob_btts(build_M(snap)) - fbtts) ** 2 for snap in _checkpoint_snaps(snaps)]))


def fit_param(builder_factory, cfg, grid, train) -> float:
    def obj(p: float) -> float:
        m = evaluate(builder_factory(cfg, p), train)
        return m["btts_ll"] + sum(m[f"o{ln}_ll"] for ln in TOTAL_LINES) / len(TOTAL_LINES)
    return min(grid, key=obj)


def boot_ci(deltas: list[float], rng: random.Random, n: int = BOOT_RESAMPLES) -> tuple[float, float, float]:
    arr = np.array(deltas)
    means = sorted(float(np.mean(arr[[rng.randrange(len(arr)) for _ in range(len(arr))]])) for _ in range(n))
    return float(arr.mean()), means[int(0.025 * n)], means[int(0.975 * n)]


# --- main ---------------------------------------------------------------------------------- #
def main() -> None:
    rng = random.Random(0)
    base = load_config(load_env=False, use_local=False).model
    data = {name: load_wc(*ids) for name, ids in WORLD_CUPS.items()}
    for name, ms in data.items():
        print(f"loaded WC {name}: {len(ms)} matches")

    # ---- premise: is the correlation actually negative, does the model over-predict BTTS? ----
    finals = [(s[-1].home_score, s[-1].away_score) for ms in data.values() for s in ms]
    h = np.array([x for x, _ in finals], float)
    a = np.array([y for _, y in finals], float)
    r = float(np.corrcoef(h, a)[0, 1])
    btts_emp = float(np.mean([(x > 0 and y > 0) for x, y in finals]))
    base_model = DixonColesInplayModel(base)
    btts_pred = float(np.mean([prob_btts(base_model.scoreline_matrix(s[0])) for ms in data.values() for s in ms]))
    print(f"\nPREMISE: r(home,away)={r:+.4f} (plan claimed -0.085; SE≈{1/math.sqrt(len(finals)-3):.3f} → ~indistinct from 0)")
    print(f"         BTTS empirical={btts_emp:.4f}  model kickoff pred={btts_pred:.4f}  (model over-predicts {btts_pred-btts_emp:+.4f})")

    # ---- held-out A/B + per-match panel for the significance bootstrap ----
    folds = [("2018", "2022"), ("2022", "2018")]
    arms = ("baseline", "rho_persist", "frank")
    agg = {arm: [] for arm in arms}
    panel = {"rho_persist": [], "frank": []}  # per-match held-out ΔBTTS-Brier vs baseline
    fitted = {"rho_persist": [], "frank": []}

    for train_name, test_name in folds:
        train, test = data[train_name], data[test_name]
        rp = fit_param(builder_rho_persist, base, DRAW_RHO_GRID, train)
        th = fit_param(builder_frank, base, THETA_GRID, train)
        fitted["rho_persist"].append(rp)
        fitted["frank"].append(th)
        builders = {"baseline": builder_baseline(base), "rho_persist": builder_rho_persist(base, rp),
                    "frank": builder_frank(base, th)}
        for arm in arms:
            agg[arm].append(evaluate(builders[arm], test))
        for snaps in test:
            bb = per_match_btts_brier(builders["baseline"], snaps)
            for arm in ("rho_persist", "frank"):
                panel[arm].append(per_match_btts_brier(builders[arm], snaps) - bb)
        print(f"\n[test={test_name}] fitted: rho_persist draw_rho={rp}  frank theta={th}")

    def mean(arm, k):
        return sum(x[k] for x in agg[arm]) / len(agg[arm])

    print("\n=== held-out averages (leave-one-tournament-out) ===")
    for arm in arms:
        print(f"  {arm:11s}: BTTS brier={mean(arm,'btts_brier'):.4f} ll={mean(arm,'btts_ll'):.4f} | "
              f"1X2 ll={mean(arm,'x12_ll'):.4f} rps={mean(arm,'x12_rps'):.4f} | "
              f"O1.5/2.5/3.5 br={mean(arm,'o1.5_brier'):.4f}/{mean(arm,'o2.5_brier'):.4f}/{mean(arm,'o3.5_brier'):.4f}")

    # ---- significance gate: ship ONLY if the match-clustered ΔBTTS-Brier CI excludes 0 ----
    print("\n=== decision (significance floor: bootstrap 95% CI of ΔBTTS-Brier must EXCLUDE 0) ===")
    winner = None
    for arm in ("rho_persist", "frank"):
        m, lo, hi = boot_ci(panel[arm], rng)
        frac = float(np.mean([d < 0 for d in panel[arm]]))
        sig = hi < 0  # significantly better
        x12_ok = (mean(arm, "x12_ll") - mean("baseline", "x12_ll") <= X12_LL_TOL
                  and mean(arm, "x12_rps") - mean("baseline", "x12_rps") <= X12_RPS_TOL)
        tot_ok = all(agg[arm][i][f"o{ln}_brier"] - agg["baseline"][i][f"o{ln}_brier"] <= TOTAL_BRIER_TOL
                     for ln in TOTAL_LINES for i in range(len(folds)))
        ok = sig and x12_ok and tot_ok
        print(f"  {arm:11s}: ΔBTTS-Brier mean={m:+.5f} 95%CI=[{lo:+.5f},{hi:+.5f}] frac_better={frac:.2f}"
              f"  sig={'YES' if sig else 'no (CI crosses 0)'}  1X2_ok={x12_ok}  totals_ok={tot_ok}  -> {'PASS' if ok else 'fail'}")
        if arm == "rho_persist" and set(fitted["rho_persist"]) and max(fitted["rho_persist"]) >= 0:
            print(f"               note: fitted draw_rho={fitted['rho_persist']} (>=0 contradicts the negative-dependence hypothesis)")
        if ok and winner is None:
            winner = arm

    if winner:
        print(f"\n  => SHIP {winner}: held-out BTTS gain is significant and non-worsening elsewhere.")
    else:
        print("\n  => KEEP OFF: no arm's held-out BTTS gain clears the significance floor (every CI crosses 0).")
        print("     The premise correlation (r=-0.03) is indistinguishable from 0 at n=128 — a null is expected.")
        print("     Re-open only with MORE tournaments (Euro/Copa) + ideally Kalshi closing prices (CLV, not just calibration).")


if __name__ == "__main__":
    main()
