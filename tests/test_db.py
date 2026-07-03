"""Persistence layer: SQLite pragmas + batched writes."""

from __future__ import annotations

from sqlalchemy import text

from wc_kalshi.models.db import Database


def test_file_sqlite_uses_wal_and_normal_sync(tmp_path):
    """A file-backed recorder DB must run in WAL + synchronous=NORMAL: default
    delete/FULL fsyncs on every one of the ~8-12 commits per match-tick, inside the
    async loop that's also placing orders."""
    db = Database(f"sqlite:///{tmp_path / 'rec.sqlite3'}")
    with db.session() as s:
        journal = s.execute(text("PRAGMA journal_mode")).scalar()
        sync = s.execute(text("PRAGMA synchronous")).scalar()
    assert str(journal).lower() == "wal"
    assert int(sync) == 1  # NORMAL


def test_memory_sqlite_still_works(tmp_path):
    """The WAL hook must not break :memory: (journal stays 'memory', not WAL)."""
    db = Database("sqlite:///:memory:")
    with db.session() as s:
        journal = s.execute(text("PRAGMA journal_mode")).scalar()
    assert str(journal).lower() in {"memory", "wal"}  # never raises


async def test_batched_tick_persists_edges_and_signals(cfg, tmp_db):
    """The batched per-tick writes (add_market_snapshots / add_edges / audit.signals)
    must persist exactly what the old per-row loop did — every edge row and a matching
    'signal' decision row."""
    from wc_kalshi.engine.builders import build_runtime
    from wc_kalshi.engine.match_loop import MatchState, TickProcessor
    from wc_kalshi.ingestion.football.simulated import simulate_full_match
    from wc_kalshi.ingestion.kalshi.feed import SimulatedMarketFeed
    from wc_kalshi.models.db import DecisionRow, MarketSnapshotRow

    cfg = cfg.model_copy(deep=True)
    cfg.risk.max_daily_loss = 1e9
    rt = build_runtime(cfg, db=tmp_db)
    proc = TickProcessor(rt, decision_mode="autonomous")
    feed = SimulatedMarketFeed(seed=7)
    st = MatchState("db-1")
    edge_total = 0
    for m in simulate_full_match(seed=4, match_id="db-1"):
        snaps = await feed.snapshots_for_match(m)
        tmp_db.add_market_snapshots(snaps)  # mirror the orchestrator's raw persist
        out = await proc.process(m, snaps, st)
        edge_total += out["edges"]

    edges = tmp_db.iter_edges("db-1")
    assert len(edges) == edge_total > 0  # every edge persisted (batched)
    with tmp_db.session() as s:
        n_signal = s.query(DecisionRow).filter(DecisionRow.kind == "signal").count()
        n_market = s.query(MarketSnapshotRow).filter(MarketSnapshotRow.match_id == "db-1").count()
    assert n_signal == edge_total  # one signal decision per edge, none dropped
    assert n_market > 0
    await rt.aclose()
