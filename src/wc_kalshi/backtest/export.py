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

import hashlib
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..config import REPO_ROOT, AppConfig
from ..models.db import Database
from ..models.schemas import MatchSnapshot
from ..modeling.derived import (
    prob_btts, prob_correct_score, prob_spread, prob_team_total_over, prob_total_over,
)
from ..modeling.inplay import DixonColesInplayModel
from .replay import Backtester, _bucket_market_by_tick

_OUTCOMES = ("home", "draw", "away")

# Scoreline-derived market series we price + settle (read-only "Markets" view).
_DERIVED_TYPES = {"KXWCTOTAL": "total", "KXWCBTTS": "btts", "KXWCSPREAD": "spread", "KXWCTEAMTOTAL": "team_total"}

# Human labels for EVERY captured Kalshi per-match series (the live "all markets" board).
# A subset is priceable by the Dixon-Coles final-score matrix; the rest (half-markets,
# corners, first-to-score) we show market-only — honestly flagged "no model price".
_SERIES_LABEL = {
    "KXWCGAME": "Match result (1X2)",
    "KXWCTOTAL": "Total goals (O/U)",
    "KXWCBTTS": "Both teams to score",
    "KXWCSPREAD": "Spread / handicap",
    "KXWCTEAMTOTAL": "Team total goals",
    "KXWCSCORE": "Correct score",
    "KXWCFTTS": "First team to score",
    "KXWCCORNERS": "Team corners",
    "KXWCTCORNERS": "Total corners",
    "KXWC1H": "1st half result",
    "KXWC2H": "2nd half result",
    "KXWC1HTOTAL": "1st half total goals",
    "KXWC2HTOTAL": "2nd half total goals",
    "KXWC1HSCORE": "1st half correct score",
    "KXWC1HSPREAD": "1st half spread",
    "KXWC2HSPREAD": "2nd half spread",
    "KXWC1HBTTS": "1st half BTTS",
    "KXWC2HBTTS": "2nd half BTTS",
}
# Series the full-match scoreline matrix can price (everything else is market-only).
_PRICEABLE_SERIES = {"KXWCTOTAL", "KXWCBTTS", "KXWCSPREAD", "KXWCTEAMTOTAL", "KXWCSCORE"}


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


def _coverage(match_snaps: list[MatchSnapshot]) -> dict[str, Any]:
    """Per-match capture quality. ``preoff_is_kickoff`` is the honesty flag: when the
    first captured snapshot is already mid-game, the "pre-off" CLV reference is really a
    mid-match line and the match should be excluded from the kickoff-only headline."""
    if not match_snaps:
        return {}
    first_min = match_snaps[0].minute
    gaps: list[float] = []
    for a, b in zip(match_snaps, match_snaps[1:]):
        try:
            gaps.append((b.ts - a.ts).total_seconds())
        except (TypeError, AttributeError):
            pass
    gaps.sort()
    p95 = gaps[min(len(gaps) - 1, int(0.95 * len(gaps)))] if gaps else None
    return {
        "first_capture_minute": first_min,
        "last_capture_minute": match_snaps[-1].minute,
        "n_snaps": len(match_snaps),
        "tick_gap_p95_seconds": round(p95, 1) if p95 is not None else None,
        "preoff_is_kickoff": bool(first_min <= 2),
    }


def _git_sha() -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=5,
        )
        return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def _provenance(cfg: AppConfig, db_path: str, stake_mode: str) -> dict[str, Any]:
    """Reproducibility envelope: which code + config + data produced these numbers.

    The model probabilities/Brier/ECE in every bundle are RECOMPUTED with the current
    model code at export time (not the values stored at capture), so without this stamp
    a later tweak to xg_proxy.py would silently change the published headline with no
    audit trail. ``config_sha256`` excludes secrets."""
    cfg_dump = cfg.model_dump(mode="json", exclude={"secrets"})
    cfg_sha = hashlib.sha256(
        json.dumps(cfg_dump, sort_keys=True, default=str).encode()
    ).hexdigest()[:16]

    fp = db_path[len("sqlite:///"):] if db_path.startswith("sqlite:///") else db_path
    db_info: dict[str, Any] = {}
    try:
        st = os.stat(fp)
        h = hashlib.sha256()
        with open(fp, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        db_info = {"name": os.path.basename(fp), "size_bytes": st.st_size, "sha256": h.hexdigest()[:16]}
    except OSError:
        pass

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "model_git_sha": _git_sha(),
        "config_sha256": cfg_sha,
        "stake_mode": stake_mode,
        "db": db_info,
        "xg_proxy": {
            "sot": cfg.model.xg_proxy_sot,
            "off": cfg.model.xg_proxy_off,
            "big_chance": cfg.model.xg_proxy_big_chance,
            "live_xg_weight": cfg.model.live_xg_weight,
        },
        "note": (
            "Model probs/Brier/ECE are RECOMPUTED with current code at export. "
            "config_sha256 + model_git_sha pin which code produced these numbers; "
            "re-export after a model change moves the headline (see golden-numbers test)."
        ),
    }


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
        "market_pool_weight": cfg.edge.market_pool_weight,
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
    cfg: AppConfig, db_path: str, out_dir: str, *, match_ids: list[str] | None = None,
    stake_mode: str = "kelly",
) -> dict[str, Any]:
    """Run the canonical replay once for golden fills, then write per-match bundles.

    ``stake_mode="fixed"`` sizes every bet equally so the per-match P&L / t-stat are
    statistically valid (Kelly compounding makes them serially dependent AND injects a
    look-ahead via the all-matches calibration factor). The CLV headline is
    stake-independent and unaffected by this choice.

    Returns the manifest dict (also written to ``manifest.json``).
    """
    src = Database(db_path if db_path.startswith("sqlite") else f"sqlite:///{db_path}")
    bt = Backtester(cfg, trade=True, stake_mode=stake_mode)
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
        cov = _coverage(match_snaps)
        bundle["coverage"] = cov
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
            "first_capture_minute": cov.get("first_capture_minute"),
            "preoff_is_kickoff": cov.get("preoff_is_kickoff"),
            "tick_gap_p95_seconds": cov.get("tick_gap_p95_seconds"),
        })

    n_kickoff = sum(1 for m in manifest if m.get("preoff_is_kickoff"))
    manifest_doc = {
        "matches": manifest,
        "aggregate": result.to_dict(),
        "config": _config_block(cfg, cfg.risk.starting_bankroll, kelly_factor),
        "provenance": _provenance(cfg, db_path, stake_mode),
        "coverage_summary": {
            "n_matches": len(manifest),
            "n_kickoff": n_kickoff,
            "n_mid_game_start": len(manifest) - n_kickoff,
            "note": (
                "preoff CLV for the "
                f"{len(manifest) - n_kickoff} mid-game-start match(es) references a "
                "mid-match line, not a true opening line — exclude them for a clean headline."
            ),
        },
    }
    (out / "manifest.json").write_text(json.dumps(manifest_doc, indent=2))
    await bt.aclose()
    return manifest_doc


def _parse_correct_score(sub: str | None, home: str, away: str) -> tuple[int, int] | None:
    """Parse a KXWCSCORE sub-title ("Egypt wins 1-0", "Draw 1-1", "IR Iran wins 2-1")
    into (home_score, away_score). The first number is the WINNER's score."""
    import re
    if not sub:
        return None
    mo = re.search(r"(\d+)\s*-\s*(\d+)", sub)
    if not mo:
        return None
    a, b = int(mo.group(1)), int(mo.group(2))
    low = sub.lower()
    if "draw" in low or "tie" in low or a == b:
        return a, a
    if home and home.lower() in low:      # home team named => home is the winner
        return a, b
    if away and away.lower() in low:      # away team named => away is the winner
        return b, a
    return None


def _market_model_prob(series, sub, strike, M, home, away):
    """Model probability for one captured contract, or None if not priceable."""
    side = _derived_side(sub, home, away)
    if series == "KXWCTOTAL" and strike is not None:
        return prob_total_over(M, strike)
    if series == "KXWCBTTS":
        return prob_btts(M)
    if series == "KXWCSPREAD" and strike is not None and side:
        return prob_spread(M, side, strike)
    if series == "KXWCTEAMTOTAL" and strike is not None and side:
        return prob_team_total_over(M, side, strike)
    if series == "KXWCSCORE":
        sc = _parse_correct_score(sub, home, away)
        if sc is not None:
            return prob_correct_score(M, sc[0], sc[1])
    return None


def _live_market_board(model, last_snap, quote_rows, home, away):
    """Exhaustive 'every possible bet' board for an in-progress match: the latest
    quote for every captured contract across all series, with the model's price
    where the final-score matrix can compute it (else market-only)."""
    if not quote_rows:
        return []
    M = model.scoreline_matrix(last_snap) if last_snap.period.is_live else None

    groups: dict[str, list] = {}
    for series, ticker, sub, strike, bid, ask in quote_rows:
        mid = round((bid + ask) / 200.0, 4) if bid is not None and ask is not None else None
        model_prob = None
        if M is not None and series in _PRICEABLE_SERIES:
            mp = _market_model_prob(series, sub, strike, M, home, away)
            model_prob = round(mp, 4) if mp is not None else None
        groups.setdefault(series, []).append({
            "label": sub or _SERIES_LABEL.get(series, series),
            "strike": strike,
            "bid": bid,
            "ask": ask,
            "mid": mid,
            "model": model_prob,
        })

    out = []
    for series in sorted(groups, key=lambda s: (s not in _PRICEABLE_SERIES, s)):
        contracts = sorted(groups[series], key=lambda c: (c["strike"] is None, c["strike"] or 0, c["label"]))
        out.append({
            "series": series,
            "label": _SERIES_LABEL.get(series, series),
            "priceable": series in _PRICEABLE_SERIES,
            "contracts": contracts,
        })
    return out


def build_live_bundle(
    cfg: AppConfig, match_id: str, match_snaps: list[MatchSnapshot], market_snaps: list,
    all_quote_rows: list | None = None,
) -> dict[str, Any] | None:
    """Bundle for an IN-PROGRESS match — same shape as a settled bundle but with
    ``outcome=None`` and ``live=True`` (the client engine leaves bets open). When
    ``all_quote_rows`` is supplied, attach the exhaustive ``all_markets`` board (every
    captured Kalshi contract + model price where computable). Returns None if the match
    is already finished / not live."""
    if not match_snaps or match_snaps[-1].period.is_finished:
        return None
    last = match_snaps[-1]
    model = DixonColesInplayModel(cfg.model)
    ticks, tickers, preoff = _build_ticks(cfg, match_snaps, market_snaps)
    first = match_snaps[0]
    ctx = first.context
    bundle = {
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
    if all_quote_rows:
        bundle["all_markets"] = _live_market_board(
            model, last, all_quote_rows, first.home_team, first.away_team
        )
    return bundle


def _read_all_quotes(db_path: str, match_id: str) -> list:
    """Latest [bid, ask] for EVERY captured contract of a match (all series), for the
    live 'all markets' board. Returns (series, ticker, sub, strike, bid, ask) rows."""
    import sqlite3
    fp = db_path[len("sqlite:///"):] if db_path.startswith("sqlite:///") else db_path
    con = sqlite3.connect(f"file:{fp}?mode=ro", uri=True)
    # Most-recent row per ticker (max ts), then its bid/ask.
    rows = con.execute(
        "SELECT q.series, q.market_ticker, q.yes_sub_title, q.floor_strike, q.yes_bid, q.yes_ask "
        "FROM raw_market_quotes q "
        "JOIN (SELECT market_ticker, MAX(ts) mts FROM raw_market_quotes "
        "      WHERE match_id=? GROUP BY market_ticker) latest "
        "  ON q.market_ticker=latest.market_ticker AND q.ts=latest.mts "
        "WHERE q.match_id=?", (match_id, match_id)).fetchall()
    con.close()
    return rows


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
            quotes = _read_all_quotes(db_path, mid)
            b = build_live_bundle(cfg, mid, snaps, src.iter_market_snapshots(mid), quotes)
            if b is not None:
                live.append((snaps[-1].ts, b))

    # Most-recently-updated match first (its bundle is the back-compat ``bundle``).
    live.sort(key=lambda x: x[0], reverse=True)
    bundles = [b for _, b in live]

    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if bundles:
        doc: dict[str, Any] = {
            "live": True, "generated_at": generated_at, "bundles": bundles, "bundle": bundles[0],
        }
    else:
        doc = {"live": False, "generated_at": generated_at, "bundles": []}
    (out / "live.json").write_text(json.dumps(doc))
    return doc
