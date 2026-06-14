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


async def main() -> int:
    ap = argparse.ArgumentParser(description="Backtest the in-play edge strategy (no keys).")
    ap.add_argument("--matches", type=int, default=100, help="synthetic matches to simulate")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--no-trade", action="store_true", help="evaluate model only, no orders")
    ap.add_argument("--replay-db", default=None, help="replay a stored session sqlite db instead")
    args = ap.parse_args()

    cfg = load_config(load_env=False)
    bt = Backtester(cfg, trade=not args.no_trade)
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
