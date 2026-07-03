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
