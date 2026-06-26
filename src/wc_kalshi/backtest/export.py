"""Export recorder DB → per-match JSON "bundles" for the web simulator.

A bundle is everything the client-side TypeScript engine (web/lib/sim) needs to
re-run the betting policy in the browser for ANY strategy config, without Python
or a server: the per-tick model probabilities + market quotes (the heavy
Dixon-Coles output, precomputed), the final 90' outcome, the pre-off reference
line for CLV, and the canonical Python "golden" fills for validation.

The model probabilities are RECOMPUTED with the current model code (so they
reflect the xG-proxy fix, like ``wck replay`` does) — not the stale values the
recorder stored at capture time. Read-only over the DB.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..config import AppConfig
from ..models.db import Database
from ..models.schemas import MatchSnapshot
from ..modeling.inplay import DixonColesInplayModel
from .replay import Backtester, _bucket_market_by_tick

_OUTCOMES = ("home", "draw", "away")


def _final(match_snaps: list[MatchSnapshot]) -> tuple[str, int, int] | None:
    """Final 90' (settled) result as (H|D|A, home_score, away_score), or None."""
    settled = [s for s in match_snaps if s.period.is_finished]
    if not settled:
        return None
    last = settled[-1]
    d = last.home_score - last.away_score
    res = "H" if d > 0 else "D" if d == 0 else "A"
    return res, last.home_score, last.away_score


def _tick_markets(bucket, carry: dict[str, list], tickers: dict[str, str]) -> dict[str, list]:
    """Latest ``[bid, ask]`` (cents) per outcome for one tick, carrying forward last
    known. Tickers are constant per match so they're collected once into ``tickers``
    and dropped from the per-tick payload (mid = (bid+ask)/200 is computed client-side)."""
    for s in bucket:
        oc = s.outcome.value if hasattr(s.outcome, "value") else str(s.outcome)
        if oc not in _OUTCOMES:
            continue
        carry[oc] = [s.yes_bid, s.yes_ask]
        tickers.setdefault(oc, s.market_ticker)
    return {oc: list(carry[oc]) for oc in _OUTCOMES if oc in carry}


def _config_block(cfg: AppConfig, bankroll: float, kelly_factor: float) -> dict[str, Any]:
    """The exact thresholds/sizer params the TS engine must mirror to reproduce fills."""
    return {
        "min_edge": cfg.edge.min_edge,
        "min_edge_after_costs": cfg.edge.min_edge_after_costs,
        "slippage_cents": cfg.edge.slippage_cents,
        "fee_coefficient": cfg.kalshi.fee_coefficient,
        "maker_fraction": cfg.kalshi.maker_fee_fraction,
        "min_price": cfg.risk.min_price,
        "max_price": cfg.risk.max_price,
        "kelly_fraction": cfg.risk.kelly_fraction,
        "max_position_per_market": cfg.risk.max_position_per_market,
        "max_exposure_per_match": cfg.risk.max_exposure_per_match,
        "min_order_contracts": cfg.risk.min_order_contracts,
        # Execution gates that shape WHEN/HOW MUCH we trade (match_loop._handle_edge).
        "min_retrade_minutes": cfg.execution.min_retrade_minutes,
        "late_taper_minutes": cfg.execution.late_taper_minutes,
        "late_taper_floor": cfg.execution.late_taper_floor,
        "bankroll": bankroll,
        "kelly_factor": round(kelly_factor, 4),
    }


def build_bundle(
    cfg: AppConfig,
    match_id: str,
    match_snaps: list[MatchSnapshot],
    market_snaps: list,
    golden_fills: list[dict],
    per_match_pnl: float,
    kelly_factor: float,
) -> dict[str, Any] | None:
    """Assemble one match's bundle. Returns None for unsettled matches."""
    final = _final(match_snaps)
    if final is None:
        return None
    res, hs_final, as_final = final
    model = DixonColesInplayModel(cfg.model)

    ticks: list[dict[str, Any]] = []
    carry: dict[str, list] = {}
    tickers: dict[str, str] = {}
    preoff: dict[str, float] = {}
    for match, bucket in _bucket_market_by_tick(match_snaps, market_snaps):
        p = model.predict(match)
        mk = _tick_markets(bucket, carry, tickers)
        for oc, ba in mk.items():
            if oc not in preoff and ba[0] is not None and ba[1] is not None:
                preoff[oc] = round((ba[0] + ba[1]) / 200.0, 4)
        ticks.append({
            "minute": match.minute,
            "period": match.period.value,
            "score": [match.home_score, match.away_score],
            "model": [round(p.p_home, 4), round(p.p_draw, 4), round(p.p_away, 4)],
            "markets": mk,
        })

    first = match_snaps[0]
    ctx = first.context
    return {
        "match_id": match_id,
        "home_team": first.home_team,
        "away_team": first.away_team,
        "home_elo": ctx.home_elo if ctx else None,
        "away_elo": ctx.away_elo if ctx else None,
        "outcome": res,
        "final_score": [hs_final, as_final],
        "tickers": tickers,
        "preoff": {oc: preoff[oc] for oc in _OUTCOMES if oc in preoff},
        "n_ticks": len(ticks),
        "ticks": ticks,
        "golden": {
            "fills": [
                {
                    "minute": f["minute"],
                    "outcome": f["outcome"],
                    "action": f["action"],
                    "contracts": f["contracts"],
                    "entry_cents": f["entry_price_cents"],
                }
                for f in golden_fills
            ],
            "n_fills": len(golden_fills),
            "pnl": round(per_match_pnl, 2),
        },
        "config": _config_block(cfg, cfg.risk.starting_bankroll, kelly_factor),
    }


async def export_bundles(
    cfg: AppConfig, db_path: str, out_dir: str, *, match_ids: list[str] | None = None
) -> dict[str, Any]:
    """Run the canonical replay once for golden fills, then write per-match bundles.

    Returns the manifest dict (also written to ``manifest.json``).
    """
    src = Database(db_path if db_path.startswith("sqlite") else f"sqlite:///{db_path}")
    bt = Backtester(cfg, trade=True, stake_mode="kelly")
    result = await bt.run_replay(src, match_ids=match_ids)
    rt = bt.rt
    kelly_factor = float(result.calibration.get("calibration_factor", 1.0))

    # Group golden fills by match and align per-match P&L to the settled order.
    fills_by_match: dict[str, list[dict]] = {}
    for f in rt.fills_log:
        fills_by_match.setdefault(f["match_id"], []).append(f)
    ids = match_ids or src.match_ids()
    settled_ids = [m for m in ids if any(s.period.is_finished for s in src.iter_match_snapshots(m))]
    pnl_by_match = dict(zip(settled_ids, result.per_match_pnl))

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    manifest: list[dict[str, Any]] = []
    for mid in settled_ids:
        match_snaps = src.iter_match_snapshots(mid)
        market_snaps = src.iter_market_snapshots(mid)
        bundle = build_bundle(
            cfg, mid, match_snaps, market_snaps,
            fills_by_match.get(mid, []), pnl_by_match.get(mid, 0.0), kelly_factor,
        )
        if bundle is None:
            continue
        (out / f"{mid}.json").write_text(json.dumps(bundle))
        manifest.append({
            "match_id": mid,
            "home_team": bundle["home_team"],
            "away_team": bundle["away_team"],
            "outcome": bundle["outcome"],
            "final_score": bundle["final_score"],
            "n_ticks": bundle["n_ticks"],
            "n_fills": bundle["golden"]["n_fills"],
        })

    manifest_doc = {
        "matches": manifest,
        "aggregate": result.to_dict(),
        "config": _config_block(cfg, cfg.risk.starting_bankroll, kelly_factor),
    }
    (out / "manifest.json").write_text(json.dumps(manifest_doc, indent=2))
    await bt.aclose()
    return manifest_doc
