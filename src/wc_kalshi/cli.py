"""Command-line entry point.

  wck run [--mode paper] [--matches N] [--ticks N] [--duration S]
          [--dashboard] [--no-trade]      run the live/paper pipeline
  wck backtest [--matches N] [--seed S] [--json out.json]   synthetic backtest (no keys)
  wck replay --db path.sqlite3 [--match-id ID]              replay a stored session
  wck historical --data hist.jsonl                          backtest on REAL xG + prices
  wck statsbomb --competition 43 --season 106 --out f.jsonl build REAL data from StatsBomb
  wck record [--source demo|prod] [--out-db f.sqlite3]      observe-only capture for replay
  wck discover-markets [--series TICKER]                    (demo/live) map WC markets
  wck doctor                                                print resolved config & checks

Defaults are safe: paper mode, simulated feed, no real orders.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sys

from .config import ConfigError, RunMode, load_config
from .logging_setup import configure_logging, get_logger

log = get_logger("cli")


def _load(args) -> "object":
    if getattr(args, "mode", None):
        import os

        os.environ["WCK_MODE"] = args.mode
    cfg = load_config()
    configure_logging(cfg.app.log_level, cfg.app.log_format)
    return cfg


# --------------------------------------------------------------------------- #
async def _cmd_run(args) -> int:
    from .engine.builders import build_runtime
    from .engine.orchestrator import Orchestrator
    from .ingestion.football.base import build_football_provider
    from .observability.alerts import Alerter

    cfg = _load(args)
    if getattr(args, "advisory", False):
        cfg.execution.decision_mode = "advisory"
    elif getattr(args, "autonomous", False):
        cfg.execution.decision_mode = "autonomous"
    if cfg.execution.decision_mode == "advisory" and not args.dashboard:
        log.warning("advisory mode without --dashboard: proposals queue but can't be approved")
    log.info(
        "starting run",
        extra={"mode": cfg.mode.value, "decision": cfg.execution.decision_mode, "dashboard": bool(args.dashboard)},
    )
    rt = build_runtime(cfg)
    if getattr(args, "sim", False) or cfg.football.provider == "simulated":
        from .ingestion.football.simulated import SimulatedFootballProvider

        provider = SimulatedFootballProvider(
            seed=cfg.football.sim_seed,
            num_matches=args.matches,
            minutes_per_tick=cfg.football.sim_minutes_per_tick,
        )
    else:
        provider = build_football_provider(cfg)
    alerter = Alerter(cfg, rt.bus)
    alerter.start()
    orch = Orchestrator(rt, provider, trade=not args.no_trade)

    # graceful shutdown
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, orch.stop)
        except NotImplementedError:  # pragma: no cover (windows)
            pass

    tasks = [asyncio.create_task(orch.run(max_ticks=args.ticks))]
    server = None
    if args.dashboard:
        import uvicorn

        from .dashboard.app import create_app

        app = create_app(rt, orch)
        server = uvicorn.Server(
            uvicorn.Config(app, host=cfg.dashboard.host, port=cfg.dashboard.port, log_level="warning")
        )
        tasks.append(asyncio.create_task(server.serve()))
        log.info("dashboard up", extra={"url": f"http://{cfg.dashboard.host}:{cfg.dashboard.port}"})

    if args.duration:
        async def _timeout() -> None:
            await asyncio.sleep(args.duration)
            orch.stop()
            if server:
                server.should_exit = True

        tasks.append(asyncio.create_task(_timeout()))

    try:
        if args.dashboard and not args.duration:
            # keep dashboard alive after the sim ends until Ctrl-C
            await tasks[0]
            log.info("matches done; dashboard still serving (Ctrl-C to exit)")
            await asyncio.gather(*tasks[1:], return_exceptions=True)
        else:
            await tasks[0]
            if server:
                server.should_exit = True
                await asyncio.gather(*tasks[1:], return_exceptions=True)
    finally:
        for t in tasks:
            t.cancel()
        await alerter.aclose()
        await rt.aclose()
    _print_summary(rt)
    return 0


def _print_summary(rt) -> None:
    ps = rt.portfolio.snapshot(rt.last_mids)
    rs = rt.risk.snapshot()
    cm = rt.calibration.metrics()
    print("\n--- RUN SUMMARY -------------------------------------------")
    print(f"  mode={rt.cfg.mode.value}  equity={ps['equity']}  realized={ps['realized_pnl']}  fees={ps['fees_paid']}")
    print(f"  halted={rs['halted']} {rs['halt_reason']}  kill_switch={rs['kill_switch']}")
    if cm["n"]:
        print(f"  calibration: n={cm['n']:.0f} brier={cm['brier']:.3f} ece={cm['ece']:.3f}")
    print("-----------------------------------------------------------")


async def _cmd_backtest(args) -> int:
    from .backtest.replay import Backtester

    cfg = _load(args)
    if getattr(args, "market_awareness", None) is not None:
        cfg.football.sim_market_xg_awareness = args.market_awareness
    bt = Backtester(
        cfg,
        trade=not args.no_trade,
        stake_mode=getattr(args, "stake_mode", "kelly"),
        fixed_stake=getattr(args, "fixed_stake", None),
    )
    res = await bt.run_synthetic(n_matches=args.matches, seed0=args.seed)
    print(res.report())
    if args.json:
        with open(args.json, "w") as fh:
            json.dump({**res.to_dict(), "reliability": res.reliability}, fh, indent=2)
        print(f"wrote {args.json}")
    await bt.aclose()
    return 0


async def _cmd_replay(args) -> int:
    from .backtest.replay import Backtester
    from .models.db import Database

    cfg = _load(args)
    if getattr(args, "bankroll", None) is not None:
        cfg.risk.starting_bankroll = args.bankroll
    source = Database(args.db if args.db.startswith("sqlite") else f"sqlite:///{args.db}")
    bt = Backtester(
        cfg,
        trade=not args.no_trade,
        stake_mode=getattr(args, "stake_mode", "kelly"),
    )
    res = await bt.run_replay(source, match_ids=[args.match_id] if args.match_id else None)
    print(res.report())
    await bt.aclose()
    return 0


async def _cmd_export(args) -> int:
    from .backtest.export import export_bundles, export_live

    cfg = _load(args)
    if getattr(args, "bankroll", None) is not None:
        cfg.risk.starting_bankroll = args.bankroll
    if getattr(args, "live", False):
        doc = await export_live(cfg, args.db, args.out)
        bundles = doc.get("bundles") or ([doc["bundle"]] if doc.get("bundle") else [])
        if doc.get("live") and bundles:
            print(f"live: {len(bundles)} match(es) → {args.out}/live.json")
            for b in bundles:
                print(f"  {b['home_team']} {b['final_score'][0]}-{b['final_score'][1]} "
                      f"{b['away_team']} ({b['minute']}')")
        else:
            print(f"no live match → wrote {args.out}/live.json (live:false)")
        upcoming = doc.get("upcoming") or []
        if upcoming:
            print(f"upcoming: {len(upcoming)} projection(s)")
            for b in upcoming:
                print(f"  {b['home_team']} v {b['away_team']} (kickoff {b.get('kickoff') or 'TBD'})")
        return 0
    ids = [args.match_id] if getattr(args, "match_id", None) else None
    manifest = await export_bundles(
        cfg, args.db, args.out, match_ids=ids, stake_mode=getattr(args, "stake_mode", "kelly")
    )
    agg = manifest["aggregate"]
    ci = agg.get("clv_ci_preoff", [0, 0])
    print(
        f"exported {len(manifest['matches'])} bundles to {args.out} "
        f"(aggregate: {agg['n_fills']} fills, CLV preoff {agg['avg_clv_preoff']:+.4f} "
        f"95% CI [{ci[0]:+.4f}, {ci[1]:+.4f}] over {agg.get('n_clusters_preoff', 0)} matches "
        f"→ {agg.get('edge_verdict', 'n/a').replace('_', ' ')})"
    )
    return 0


async def _cmd_historical(args) -> int:
    from .backtest.historical import load_historical_file
    from .backtest.replay import Backtester

    cfg = _load(args)
    matches = load_historical_file(args.data)
    if not matches:
        print(f"no historical matches found in {args.data}")
        return 1
    bt = Backtester(
        cfg,
        trade=not args.no_trade,
        stake_mode=getattr(args, "stake_mode", "kelly"),
        fixed_stake=getattr(args, "fixed_stake", None),
    )
    res = await bt.run_historical(matches)
    print(res.report())
    await bt.aclose()
    return 0


async def _cmd_discover(args) -> int:
    from .engine.builders import _build_kalshi_client

    cfg = _load(args)
    if cfg.is_paper:
        print("discover-markets needs demo/live mode + Kalshi credentials (set WCK_MODE=demo).")
        return 1
    client = _build_kalshi_client(cfg)
    try:
        series = args.series or cfg.kalshi.worldcup_series_ticker
        payload = await client.get_events(series_ticker=series, status="open")
        events = payload.get("events", [])
        print(f"Found {len(events)} events for series {series!r}:")
        for ev in events[:50]:
            title = ev.get("title") or ev.get("sub_title")
            n_markets = len(ev.get("markets", []))
            print(f"  {ev.get('event_ticker'):<28} {title}  ({n_markets} markets)")
    finally:
        await client.aclose()
    return 0


async def _cmd_fit(args) -> int:
    from .backtest.historical import load_historical_file
    from .ingestion.football.simulated import FIXTURES, simulate_full_match
    from .modeling.fit import fit_constants

    cfg = _load(args)
    if args.data:
        matches = [[snap for snap, _mk in ticks] for ticks in load_historical_file(args.data)]
        src = args.data
    else:
        matches = [
            simulate_full_match(seed=s, fixture=FIXTURES[s % len(FIXTURES)], match_id=f"fit-{s}")
            for s in range(args.synthetic)
        ]
        src = f"{args.synthetic} synthetic matches (NOT real data — demo only)"
    if not matches:
        print("no matches to fit on")
        return 1
    res = fit_constants(matches, cfg.model)
    print(f"fit on {src}")
    print(f"  samples:        {res.n_samples}")
    print(f"  logloss before: {res.logloss_before:.4f}")
    print(f"  logloss after:  {res.logloss_after:.4f}")
    print("  fitted constants (paste into config/local.yaml):")
    print(res.yaml_snippet())
    return 0


def _cmd_statsbomb(args) -> int:
    """Build a real historical dataset from StatsBomb open data (+ optional Betfair)."""
    from pathlib import Path

    from .backtest.statsbomb import build_world_cup, coverage_report

    elo_table = None
    if args.elo_table:
        raw = json.loads(Path(args.elo_table).read_text())
        elo_table = {k: float(v) for k, v in raw.items()}

    matches = build_world_cup(
        args.competition,
        args.season,
        repo=args.repo,
        elo_table=elo_table,
        settle_minute=args.settle_minute,
        limit=args.limit,
        allow_builtin_fallback=getattr(args, "allow_anachronistic_elo", False),
    )
    if not matches:
        print("no matches converted (no per-shot xG for this season?)", file=sys.stderr)
        return 1

    if args.betfair:
        from .backtest.betfair import merge_markets, parse_stream_path

        timelines = parse_stream_path(args.betfair)
        matches, report = merge_markets(matches, timelines, tolerance=args.betfair_tolerance)
        print(f"betfair: {report.summary()}", file=sys.stderr)
    else:
        print(
            "no --betfair: xG-only dataset -> wck historical measures CALIBRATION, not CLV.",
            file=sys.stderr,
        )

    print(f"coverage: {coverage_report(matches)}", file=sys.stderr)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w") as fh:
        for m in matches:
            fh.write(json.dumps(m) + "\n")
    print(f"wrote {len(matches)} matches -> {out}")
    print(f"next: wck historical --data {out} --stake-mode fixed")
    return 0


async def _cmd_record(args) -> int:
    """Observe-only run: log real xG + read-only Kalshi prices to a DB for later replay.

    Never trades and never reaches live execution. ``demo`` (default) records real Kalshi
    *demo* prices; ``prod`` points the read-only market feed at the prod base (needs prod
    read creds + listed WC markets — currently none). Replay later with ``wck replay``.
    """
    import os

    from .engine.builders import build_runtime
    from .engine.orchestrator import Orchestrator
    from .ingestion.football.base import build_football_provider
    from .models.db import Database

    os.environ["WCK_MODE"] = "demo"  # observe-only; never live
    cfg = _load(args)
    if args.source == "prod":
        cfg.kalshi.rest_base_demo = cfg.kalshi.rest_base_prod  # read-only prod market data
        log.warning("record: using PROD REST base read-only (needs prod read creds + live WC markets)")

    db = Database(args.out_db if args.out_db.startswith("sqlite") else f"sqlite:///{args.out_db}")
    rt = build_runtime(cfg, db=db)
    provider = build_football_provider(cfg)
    orch = Orchestrator(rt, provider, trade=False)  # NEVER trade while recording

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, orch.stop)
        except NotImplementedError:  # pragma: no cover (windows)
            pass

    task = asyncio.create_task(orch.run(max_ticks=args.ticks))
    if args.duration:
        async def _timeout() -> None:
            await asyncio.sleep(args.duration)
            orch.stop()

        asyncio.create_task(_timeout())  # noqa: RUF006
    try:
        await task
    finally:
        await rt.aclose()

    ids = db.match_ids()
    n_rows = sum(len(db.iter_match_snapshots(i)) for i in ids)
    print("\n--- RECORD SUMMARY ----------------------------------------")
    print(f"  db:        {args.out_db}")
    print(f"  matches:   {len(ids)}  match snapshots: {n_rows}")
    print(f"  replay:    wck replay --db {args.out_db}")
    print("-----------------------------------------------------------")
    return 0


def _cmd_doctor(args) -> int:
    try:
        cfg = _load(args)
    except ConfigError as exc:
        print(f"CONFIG ERROR: {exc}")
        return 1
    from .config import LOCAL_CONFIG

    creds = cfg.secrets.has_kalshi_creds()
    local_present = LOCAL_CONFIG.exists()
    print("wck doctor")
    print(f"  mode:              {cfg.mode.value}")
    print(f"  config/local.yaml: {'present' if local_present else 'MISSING'}")
    print(f"  kalshi rest base:  {cfg.kalshi_rest_base}")
    print(f"  football provider: {cfg.football.provider}")
    print(f"  fee_coefficient:   {cfg.kalshi.fee_coefficient}")
    print(f"  capture_extra:     {cfg.kalshi.capture_extra_markets}")
    print(f"  db:                {cfg.resolved_db_url()}")
    print(f"  kalshi creds set:  {creds}")
    if not local_present and cfg.football.provider == "apifootball":
        print("  WARNING: provider=apifootball without config/local.yaml — fee + extra-market "
              "capture fell back to defaults (phantom 0.07 fee, 1X2-only capture).")
    print(f"  apifootball key:   {bool(cfg.secrets.apifootball_key)}")
    print(f"  kelly fraction:    {cfg.risk.kelly_fraction}")
    print(f"  edge thresholds:   min={cfg.edge.min_edge} after_costs={cfg.edge.min_edge_after_costs}")
    print(f"  guardrails:        max_match=${cfg.risk.max_exposure_per_match} daily_loss=${cfg.risk.max_daily_loss}")
    if cfg.mode is not RunMode.PAPER and not creds:
        print("  WARNING: non-paper mode without Kalshi credentials — execution will fail.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="wck", description="World Cup × Kalshi in-play edge engine")
    p.add_argument("--mode", choices=["paper", "demo", "live"], help="override run mode")
    sub = p.add_subparsers(dest="command", required=True)

    r = sub.add_parser("run", help="run the live/paper pipeline")
    r.add_argument("--matches", type=int, default=3, help="simulated matches (simulated provider)")
    r.add_argument("--ticks", type=int, default=None, help="max poll ticks then stop")
    r.add_argument("--duration", type=float, default=None, help="auto-stop after N seconds")
    r.add_argument("--dashboard", action="store_true", help="serve the web dashboard")
    r.add_argument("--no-trade", action="store_true", help="observe only, place no orders")
    r.add_argument("--sim", action="store_true", help="force the built-in simulator (ignore the live feed)")
    g = r.add_mutually_exclusive_group()
    g.add_argument("--advisory", action="store_true", help="propose trades; you approve/reject them in the dashboard")
    g.add_argument("--autonomous", action="store_true", help="auto-execute trades (overrides config)")

    b = sub.add_parser("backtest", help="synthetic backtest (no keys)")
    b.add_argument("--matches", type=int, default=100)
    b.add_argument("--seed", type=int, default=0)
    b.add_argument("--no-trade", action="store_true")
    b.add_argument("--json", type=str, default=None, help="write JSON result here")
    b.add_argument(
        "--stake-mode", choices=["kelly", "fixed"], default="kelly",
        help="fixed = constant stake per bet so the t-stat/CI are statistically valid",
    )
    b.add_argument("--fixed-stake", type=float, default=None, help="$ stake per bet in fixed mode")
    b.add_argument(
        "--market-awareness", type=float, default=None,
        help="simulated market xG awareness 0..1 (0=blind strawman, 1=sharp); higher shrinks edge",
    )

    rp = sub.add_parser("replay", help="replay a stored session DB (paper bets)")
    rp.add_argument("--db", required=True, help="path to a sqlite db from a prior run")
    rp.add_argument("--match-id", default=None)
    rp.add_argument("--no-trade", action="store_true")
    rp.add_argument("--bankroll", type=float, default=None, help="starting fake bankroll $ (e.g. 100)")
    rp.add_argument("--stake-mode", choices=["kelly", "fixed"], default="kelly")

    ex = sub.add_parser("export-bundles", help="export DB → per-match JSON bundles for the web simulator")
    ex.add_argument("--db", required=True, help="path to a recorder sqlite db")
    ex.add_argument("--out", default="web/public/bundles", help="output dir for bundles + manifest")
    ex.add_argument("--match-id", default=None, help="export a single match (e.g. for live)")
    ex.add_argument("--bankroll", type=float, default=None, help="starting fake bankroll $ (default cfg)")
    ex.add_argument(
        "--stake-mode", choices=["kelly", "fixed"], default="kelly",
        help="fixed = equal stake per bet → look-ahead-free, statistically valid P&L/CI",
    )
    ex.add_argument("--live", action="store_true", help="write live.json for all in-progress matches only")

    h = sub.add_parser("historical", help="backtest against REAL xG + market data (JSON)")
    h.add_argument("--data", required=True, help="path to historical JSON/JSONL (see backtest/historical.py)")
    h.add_argument("--no-trade", action="store_true")
    h.add_argument("--stake-mode", choices=["kelly", "fixed"], default="kelly")
    h.add_argument("--fixed-stake", type=float, default=None)

    d = sub.add_parser("discover-markets", help="map WC markets via Kalshi (demo/live)")
    d.add_argument("--series", default=None)

    f = sub.add_parser("fit", help="fit model constants to data (real or synthetic)")
    f.add_argument("--data", default=None, help="historical JSON/JSONL; omit to fit on synthetic")
    f.add_argument("--synthetic", type=int, default=200, help="synthetic matches if --data omitted")

    sb = sub.add_parser("statsbomb", help="build a REAL historical dataset from StatsBomb open data")
    sb.add_argument("--competition", type=int, default=43, help="StatsBomb competition_id (43=men's WC)")
    sb.add_argument("--season", type=int, default=106, help="StatsBomb season_id (106=2022, 3=2018)")
    sb.add_argument("--out", required=True, help="output historical JSONL path")
    sb.add_argument("--repo", default=None, help="local clone of statsbomb/open-data (offline)")
    sb.add_argument("--elo-table", default=None, help="JSON {team: elo} for date-appropriate priors")
    sb.add_argument("--allow-anachronistic-elo", action="store_true",
                    help="fall back to built-in 2026 ratings when no --elo-table (WRONG era; off by default)")
    sb.add_argument("--betfair", default=None, help="Betfair historical file/dir to merge for CLV")
    sb.add_argument("--betfair-tolerance", type=int, default=2, help="max minutes when snapping quotes to ticks")
    sb.add_argument("--settle-minute", type=int, default=90, help="regulation settlement minute")
    sb.add_argument("--limit", type=int, default=None, help="convert only the first N matches")

    rec = sub.add_parser("record", help="observe-only: log real xG + read-only Kalshi prices to a DB")
    rec.add_argument("--out-db", default="data/record.sqlite3", help="sqlite db to write")
    rec.add_argument("--source", choices=["demo", "prod"], default="demo", help="demo prices, or read-only prod base")
    rec.add_argument("--duration", type=float, default=None, help="auto-stop after N seconds")
    rec.add_argument("--ticks", type=int, default=None, help="auto-stop after N poll ticks")

    sub.add_parser("doctor", help="print resolved config and checks")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "run":
            return asyncio.run(_cmd_run(args))
        if args.command == "backtest":
            return asyncio.run(_cmd_backtest(args))
        if args.command == "replay":
            return asyncio.run(_cmd_replay(args))
        if args.command == "historical":
            return asyncio.run(_cmd_historical(args))
        if args.command == "export-bundles":
            return asyncio.run(_cmd_export(args))
        if args.command == "fit":
            return asyncio.run(_cmd_fit(args))
        if args.command == "statsbomb":
            return _cmd_statsbomb(args)
        if args.command == "record":
            return asyncio.run(_cmd_record(args))
        if args.command == "discover-markets":
            return asyncio.run(_cmd_discover(args))
        if args.command == "doctor":
            return _cmd_doctor(args)
    except ConfigError as exc:
        print(f"CONFIG ERROR: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
