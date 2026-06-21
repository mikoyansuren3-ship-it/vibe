#!/usr/bin/env python3
"""Standalone backtest/replay script — runs on synthetic or stored data, NO live keys.

Works without installing the package (adds ./src to sys.path), so a grader can do:

    python scripts/backtest.py --matches 200
    python scripts/backtest.py --replay-db data/wck.sqlite3
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from wc_kalshi.backtest.replay import Backtester  # noqa: E402
from wc_kalshi.config import load_config  # noqa: E402
from wc_kalshi.models.db import Database  # noqa: E402


async def _run_sweep(cfg, args) -> int:
    """Sweep the simulated market's xG awareness and show the edge collapse as the
    counterparty gets sharper — the honest antidote to the circular backtest."""
    print(f"{'awareness':>10} {'realized':>10} {'roi%':>8} {'avg_clv':>9} {'fills':>7}")
    for aware in (0.0, 0.25, 0.5, 0.75, 1.0):
        c = cfg.model_copy(deep=True)
        c.football.sim_market_xg_awareness = aware
        bt = Backtester(c, trade=True, stake_mode=args.stake_mode, fixed_stake=args.fixed_stake)
        res = await bt.run_synthetic(n_matches=args.matches, seed0=args.seed)
        await bt.aclose()
        print(
            f"{aware:>10.2f} {res.realized_pnl:>10.2f} {res.roi*100:>8.1f} "
            f"{res.avg_clv:>9.4f} {res.n_fills:>7d}"
        )
    return 0


async def main() -> int:
    ap = argparse.ArgumentParser(description="Backtest the in-play edge strategy (no keys).")
    ap.add_argument("--matches", type=int, default=100, help="synthetic matches to simulate")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--no-trade", action="store_true", help="evaluate model only, no orders")
    ap.add_argument("--replay-db", default=None, help="replay a stored session sqlite db instead")
    ap.add_argument("--stake-mode", choices=["kelly", "fixed"], default="kelly")
    ap.add_argument("--fixed-stake", type=float, default=None)
    ap.add_argument("--market-awareness", type=float, default=None, help="sim market xG awareness 0..1")
    ap.add_argument("--sweep", action="store_true", help="sweep market awareness 0..1 and print a table")
    args = ap.parse_args()

    cfg = load_config(load_env=False)
    if args.sweep:
        return await _run_sweep(cfg, args)
    if args.market_awareness is not None:
        cfg.football.sim_market_xg_awareness = args.market_awareness
    bt = Backtester(
        cfg, trade=not args.no_trade, stake_mode=args.stake_mode, fixed_stake=args.fixed_stake
    )
    if args.replay_db:
        src = args.replay_db
        db = Database(src if src.startswith("sqlite") else f"sqlite:///{src}")
        res = await bt.run_replay(db)
    else:
        res = await bt.run_synthetic(n_matches=args.matches, seed0=args.seed)
    print(res.report())
    await bt.aclose()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
