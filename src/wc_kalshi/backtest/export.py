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
from ..modeling.derived import (
    prob_btts, prob_spread, prob_team_total_over, prob_total_over,
)
from ..modeling.inplay import DixonColesInplayModel
from .replay import Backtester, _bucket_market_by_tick

_OUTCOMES = ("home", "draw", "away")

# Scoreline-derived market series we price + settle (read-only "Markets" view).
_DERIVED_TYPES = {"KXWCTOTAL": "total", "KXWCBTTS": "btts", "KXWCSPREAD": "spread", "KXWCTEAMTOTAL": "team_total"}


def _derived_side(sub: str | None, home: str, away: str) -> str | None:
    if sub and home and home in sub:
        return "home"
    if sub and away and away in sub:
        return "away"
    return None


def _derived_model_prob(series, M, strike, sub, home, away):
    side = _derived_side(sub, home, away)
    if series == "KXWCTOTAL" and strike is not None:
        return prob_total_over(M, strike)
    if series == "KXWCBTTS":
        return prob_btts(M)
    if series == "KXWCSPREAD" and strike is not None and side:
        return prob_spread(M, side, strike)
    if series == "KXWCTEAMTOTAL" and strike is not None and side:
        return prob_team_total_over(M, side, strike)
    return None


def _derived_settles_yes(series, strike, sub, home, away, hs, as_):
    side_is_home = bool(sub and home and home in sub)
    side, opp = (hs, as_) if side_is_home else (as_, hs)
    if series == "KXWCTOTAL":
        return (hs + as_) > strike
    if series == "KXWCBTTS":
        return hs > 0 and as_ > 0
    if series == "KXWCSPREAD":
        return (side - opp) > strike
    if series == "KXWCTEAMTOTAL":
        return side > strike
    return None


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


def _build_ticks(
    cfg: AppConfig, match_snaps: list[MatchSnapshot], market_snaps: list
) -> tuple[list[dict[str, Any]], dict[str, str], dict[str, float]]:
    """Per-tick model probs + compact market quotes + pre-off mids (shared by
    settled and live bundle builders)."""
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
    return ticks, tickers, {oc: preoff[oc] for oc in _OUTCOMES if oc in preoff}


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
    ticks, tickers, preoff = _build_ticks(cfg, match_snaps, market_snaps)

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


def _build_derived(model, match_snaps, quote_rows, home, away, hs_final, as_final):
    """Time series of (minute, model_prob, market_mid) for each scoreline-derived
    contract, downsampled to ~1/2min, with settlement. Read-only (no betting)."""
    import bisect

    live = [s for s in match_snaps if s.period.is_live]
    if not live or not quote_rows:
        return []
    ts_list = [s.ts.replace(tzinfo=None) for s in live]
    mat_cache: dict[int, Any] = {}

    def matrix_at(ts):
        i = bisect.bisect_right(ts_list, ts) - 1
        if i < 0:
            return None, None
        if i not in mat_cache:
            mat_cache[i] = model.scoreline_matrix(live[i])
        return mat_cache[i], live[i].minute

    by_ticker: dict[str, list] = {}
    meta: dict[str, tuple] = {}
    for series, tk, ts, mid, strike, sub in quote_rows:
        by_ticker.setdefault(tk, []).append((ts, mid))
        meta[tk] = (series, strike, sub)

    out = []
    for tk, quotes in by_ticker.items():
        series, strike, sub = meta[tk]
        ticks, last_min = [], -99
        for ts, mid in quotes:
            M, minute = matrix_at(ts)
            if M is None or minute - last_min < 2:  # downsample ~1/2min
                continue
            mp = _derived_model_prob(series, M, strike, sub, home, away)
            if mp is None:
                continue
            ticks.append([minute, round(mp, 3), round(mid, 3)])
            last_min = minute
        if not ticks:
            continue
        out.append({
            "type": _DERIVED_TYPES[series],
            "label": sub or _DERIVED_TYPES[series],
            "strike": strike,
            "ticks": ticks,  # [minute, model_prob, market_mid]
            "settled_yes": bool(_derived_settles_yes(series, strike, sub, home, away, hs_final, as_final)),
        })
    return out


def _read_derived_quotes(db_path: str, match_id: str):
    """Raw derived-market quotes for one match (ts parsed to naive datetime)."""
    import sqlite3
    from datetime import datetime
    fp = db_path[len("sqlite:///"):] if db_path.startswith("sqlite:///") else db_path
    con = sqlite3.connect(f"file:{fp}?mode=ro", uri=True)
    rows = con.execute(
        "SELECT series, market_ticker, ts, yes_bid, yes_ask, floor_strike, yes_sub_title "
        "FROM raw_market_quotes WHERE match_id=? AND yes_bid IS NOT NULL AND yes_ask IS NOT NULL "
        "ORDER BY ts", (match_id,)).fetchall()
    con.close()
    out = []
    for series, tk, ts, yb, ya, strike, sub in rows:
        if series not in _DERIVED_TYPES:
            continue
        out.append((series, tk, datetime.fromisoformat(ts).replace(tzinfo=None),
                    (yb + ya) / 200.0, strike, sub))
    return out


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
        # Scoreline-derived markets (Total/BTTS/Spread/TeamTotal) — only the games whose
        # raw_market_quotes were captured; read-only model-vs-market for the Markets view.
        qrows = _read_derived_quotes(db_path, mid)
        if qrows:
            fs = bundle["final_score"]
            bundle["derived"] = _build_derived(
                DixonColesInplayModel(cfg.model), match_snaps, qrows,
                bundle["home_team"], bundle["away_team"], fs[0], fs[1],
            )
        (out / f"{mid}.json").write_text(json.dumps(bundle))
        manifest.append({
            "match_id": mid,
            "home_team": bundle["home_team"],
            "away_team": bundle["away_team"],
            "outcome": bundle["outcome"],
            "final_score": bundle["final_score"],
            "n_ticks": bundle["n_ticks"],
            "n_fills": bundle["golden"]["n_fills"],
            "has_derived": bool(bundle.get("derived")),
        })

    manifest_doc = {
        "matches": manifest,
        "aggregate": result.to_dict(),
        "config": _config_block(cfg, cfg.risk.starting_bankroll, kelly_factor),
    }
    (out / "manifest.json").write_text(json.dumps(manifest_doc, indent=2))
    await bt.aclose()
    return manifest_doc


def build_live_bundle(
    cfg: AppConfig, match_id: str, match_snaps: list[MatchSnapshot], market_snaps: list
) -> dict[str, Any] | None:
    """Bundle for an IN-PROGRESS match — same shape as a settled bundle but with
    ``outcome=None`` and ``live=True`` (the client engine leaves bets open). Returns
    None if the match is already finished / not live."""
    if not match_snaps or match_snaps[-1].period.is_finished:
        return None
    last = match_snaps[-1]
    ticks, tickers, preoff = _build_ticks(cfg, match_snaps, market_snaps)
    first = match_snaps[0]
    ctx = first.context
    return {
        "match_id": match_id,
        "home_team": first.home_team,
        "away_team": first.away_team,
        "home_elo": ctx.home_elo if ctx else None,
        "away_elo": ctx.away_elo if ctx else None,
        "outcome": None,
        "live": True,
        "status": last.status,
        "minute": last.minute,
        "final_score": [last.home_score, last.away_score],
        "tickers": tickers,
        "preoff": preoff,
        "n_ticks": len(ticks),
        "ticks": ticks,
        "golden": {"fills": [], "n_fills": 0, "pnl": 0},
        "config": _config_block(cfg, cfg.risk.starting_bankroll, 1.0),
    }


def export_live(cfg: AppConfig, db_path: str, out_dir: str) -> dict[str, Any]:
    """Write ``live.json`` for EVERY currently in-progress match (or ``{live:false}``
    if none). Read-only; no Backtester needed (the client engine bets client-side).

    The doc carries ``bundles`` (all live games, most-recently-updated first) plus
    ``bundle`` (the first of those) for backward compatibility with older clients."""
    src = Database(db_path if db_path.startswith("sqlite") else f"sqlite:///{db_path}")
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    live: list[tuple[Any, dict[str, Any]]] = []
    for mid in src.match_ids():
        snaps = src.iter_match_snapshots(mid)
        if snaps and snaps[-1].period.is_live and not snaps[-1].period.is_finished:
            b = build_live_bundle(cfg, mid, snaps, src.iter_market_snapshots(mid))
            if b is not None:
                live.append((snaps[-1].ts, b))

    # Most-recently-updated match first (its bundle is the back-compat ``bundle``).
    live.sort(key=lambda x: x[0], reverse=True)
    bundles = [b for _, b in live]

    if bundles:
        doc: dict[str, Any] = {"live": True, "bundles": bundles, "bundle": bundles[0]}
    else:
        doc = {"live": False, "bundles": []}
    (out / "live.json").write_text(json.dumps(doc))
    return doc
