"""Persistence layer (SQLAlchemy 2.0).

Append-only storage so any run is fully replayable/auditable afterward. Each row
keeps a few *promoted* indexed columns for fast filtering plus a full ``data`` JSON
blob of the normalized pydantic object. Schema uses only portable types so the same
models run on Postgres by changing the URL.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    Integer,
    String,
    create_engine,
    event,
    func,
    select,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from ..util import utcnow
from .schemas import EdgeSignal, MarketSnapshot, MatchSnapshot, Probabilities


class Base(DeclarativeBase):
    pass


class MatchSnapshotRow(Base):
    __tablename__ = "match_snapshots"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    match_id: Mapped[str] = mapped_column(String(64), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    minute: Mapped[int] = mapped_column(Integer)
    period: Mapped[str] = mapped_column(String(8))
    home_score: Mapped[int] = mapped_column(Integer)
    away_score: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16))
    data: Mapped[dict[str, Any]] = mapped_column(JSON)


class MarketSnapshotRow(Base):
    __tablename__ = "market_snapshots"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    match_id: Mapped[str] = mapped_column(String(64), index=True)
    market_ticker: Mapped[str] = mapped_column(String(96), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    outcome: Mapped[str] = mapped_column(String(8))
    yes_bid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    yes_ask: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_price: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(16))
    data: Mapped[dict[str, Any]] = mapped_column(JSON)


class RawMarketQuoteRow(Base):
    """Generic capture of ANY per-match Kalshi market (Total/Spread/BTTS/1H/corners/…),
    not just 1X2. Stores the structured strike so each series can be modelled later.
    Separate from MarketSnapshotRow (which is the typed 1X2 the live strategy trades)."""

    __tablename__ = "raw_market_quotes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    match_id: Mapped[str] = mapped_column(String(64), index=True)
    series: Mapped[str] = mapped_column(String(32), index=True)
    market_ticker: Mapped[str] = mapped_column(String(96), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    yes_sub_title: Mapped[str | None] = mapped_column(String(96), nullable=True)
    floor_strike: Mapped[float | None] = mapped_column(Float, nullable=True)
    strike_type: Mapped[str | None] = mapped_column(String(24), nullable=True)
    yes_bid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    yes_ask: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_price: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(16))
    data: Mapped[dict[str, Any]] = mapped_column(JSON)


class ProbabilityRow(Base):
    __tablename__ = "probabilities"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    match_id: Mapped[str] = mapped_column(String(64), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    source: Mapped[str] = mapped_column(String(48))
    p_home: Mapped[float] = mapped_column(Float)
    p_draw: Mapped[float] = mapped_column(Float)
    p_away: Mapped[float] = mapped_column(Float)
    data: Mapped[dict[str, Any]] = mapped_column(JSON)


class EdgeRow(Base):
    __tablename__ = "edge_signals"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    match_id: Mapped[str] = mapped_column(String(64), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    outcome: Mapped[str] = mapped_column(String(8))
    market_ticker: Mapped[str] = mapped_column(String(96), index=True)
    model_prob: Mapped[float] = mapped_column(Float)
    market_prob: Mapped[float] = mapped_column(Float)
    net_edge: Mapped[float] = mapped_column(Float)
    actionable: Mapped[bool] = mapped_column(Boolean)
    action: Mapped[str | None] = mapped_column(String(8), nullable=True)
    data: Mapped[dict[str, Any]] = mapped_column(JSON)


class OrderRow(Base):
    __tablename__ = "orders"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    client_order_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    exchange_order_id: Mapped[str | None] = mapped_column(String(96), nullable=True)
    match_id: Mapped[str] = mapped_column(String(64), index=True)
    market_ticker: Mapped[str] = mapped_column(String(96), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    action: Mapped[str] = mapped_column(String(8))
    side: Mapped[str] = mapped_column(String(8))
    count: Mapped[int] = mapped_column(Integer)
    price_cents: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16))
    mode: Mapped[str] = mapped_column(String(8))
    data: Mapped[dict[str, Any]] = mapped_column(JSON)


class FillRow(Base):
    __tablename__ = "fills"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    client_order_id: Mapped[str] = mapped_column(String(64), index=True)
    match_id: Mapped[str] = mapped_column(String(64), index=True)
    market_ticker: Mapped[str] = mapped_column(String(96), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    action: Mapped[str] = mapped_column(String(8))
    side: Mapped[str] = mapped_column(String(8))
    count: Mapped[int] = mapped_column(Integer)
    price_cents: Mapped[int] = mapped_column(Integer)
    fee: Mapped[float] = mapped_column(Float)
    data: Mapped[dict[str, Any]] = mapped_column(JSON)


class DecisionRow(Base):
    """Full audit trail of signals, decisions, guardrail trips, and alerts."""

    __tablename__ = "decisions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    match_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)
    message: Mapped[str] = mapped_column(String(512))
    data: Mapped[dict[str, Any]] = mapped_column(JSON)


def _enable_sqlite_wal(engine: Any) -> None:
    """Put SQLite in WAL + ``synchronous=NORMAL`` on every connection.

    Default ``journal_mode=delete`` + ``synchronous=FULL`` fsyncs on every commit, and
    the recorder does ~8-12 single-row commits per match-tick *inside the async event
    loop* — so each fsync stalls the loop that's also polling markets and placing orders.
    WAL lets readers (the 60s live publisher / dashboard) run without blocking the writer,
    and NORMAL drops the per-commit fsync to one per checkpoint (WAL keeps this crash-safe
    for anything but an OS/power failure — acceptable for append-only research capture).
    No-op for ``:memory:`` (journal stays ``memory``) and never reached for Postgres.
    """

    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_conn: Any, _record: Any) -> None:  # pragma: no cover - trivial
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.close()


class Database:
    """Thin wrapper around the engine + session factory with typed inserts."""

    def __init__(self, db_url: str, *, echo: bool = False) -> None:
        # Defensively ensure a sqlite file's parent directory exists.
        if db_url.startswith("sqlite:///") and ":memory:" not in db_url:
            from pathlib import Path

            Path(db_url[len("sqlite:///"):]).parent.mkdir(parents=True, exist_ok=True)
        connect_args = {"check_same_thread": False} if db_url.startswith("sqlite") else {}
        self.engine = create_engine(db_url, echo=echo, future=True, connect_args=connect_args)
        if db_url.startswith("sqlite"):
            _enable_sqlite_wal(self.engine)
        self._sessionmaker = sessionmaker(self.engine, expire_on_commit=False, future=True)
        Base.metadata.create_all(self.engine)

    @contextmanager
    def session(self) -> Iterator[Session]:
        s = self._sessionmaker()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    # -- typed inserts --------------------------------------------------- #
    def add_match_snapshot(self, snap: MatchSnapshot) -> None:
        with self.session() as s:
            s.add(
                MatchSnapshotRow(
                    match_id=snap.match_id,
                    ts=snap.ts,
                    minute=snap.minute,
                    period=snap.period.value,
                    home_score=snap.home_score,
                    away_score=snap.away_score,
                    status=snap.status,
                    data=snap.model_dump(mode="json"),
                )
            )

    @staticmethod
    def _market_row(snap: MarketSnapshot) -> "MarketSnapshotRow":
        return MarketSnapshotRow(
            match_id=snap.match_id,
            market_ticker=snap.market_ticker,
            ts=snap.ts,
            outcome=snap.outcome.value,
            yes_bid=snap.yes_bid,
            yes_ask=snap.yes_ask,
            last_price=snap.last_price,
            status=snap.status,
            data=snap.model_dump(mode="json"),
        )

    def add_market_snapshot(self, snap: MarketSnapshot) -> None:
        with self.session() as s:
            s.add(self._market_row(snap))

    def add_market_snapshots(self, snaps: list[MarketSnapshot]) -> None:
        """One transaction for a tick's whole market book (3 outcomes) instead of one
        fsync-costing commit per outcome — see WAL note on ``_enable_sqlite_wal``."""
        if not snaps:
            return
        with self.session() as s:
            s.add_all([self._market_row(x) for x in snaps])

    def add_raw_market_quotes(self, rows: list[dict[str, Any]]) -> None:
        """Batch-insert generic market quotes (see RawMarketQuoteRow / extra_markets.py)."""
        if not rows:
            return
        with self.session() as s:
            s.add_all([RawMarketQuoteRow(**r) for r in rows])

    def add_probabilities(self, probs: Probabilities) -> None:
        with self.session() as s:
            s.add(
                ProbabilityRow(
                    match_id=probs.match_id,
                    ts=probs.ts,
                    source=probs.source,
                    p_home=probs.p_home,
                    p_draw=probs.p_draw,
                    p_away=probs.p_away,
                    data=probs.model_dump(mode="json"),
                )
            )

    @staticmethod
    def _edge_row(edge: EdgeSignal) -> "EdgeRow":
        return EdgeRow(
            match_id=edge.match_id,
            ts=edge.ts,
            outcome=edge.outcome.value,
            market_ticker=edge.market_ticker,
            model_prob=edge.model_prob,
            market_prob=edge.market_prob,
            net_edge=edge.net_edge,
            actionable=edge.actionable,
            action=edge.action.value if edge.action else None,
            data=edge.model_dump(mode="json"),
        )

    def add_edge(self, edge: EdgeSignal) -> None:
        with self.session() as s:
            s.add(self._edge_row(edge))

    def add_edges(self, edges: list[EdgeSignal]) -> None:
        """One transaction for a tick's 1X2 edge signals instead of one per outcome."""
        if not edges:
            return
        with self.session() as s:
            s.add_all([self._edge_row(x) for x in edges])

    def record_decision(
        self,
        kind: str,
        message: str,
        *,
        match_id: str | None = None,
        data: dict[str, Any] | None = None,
        ts: datetime | None = None,
    ) -> None:
        self.record_decisions([
            {"kind": kind, "message": message, "match_id": match_id, "data": data, "ts": ts}
        ])

    def record_decisions(self, records: list[dict[str, Any]]) -> None:
        """Batch-insert audit decision rows (one tick's signals) in a single transaction."""
        if not records:
            return
        with self.session() as s:
            s.add_all([
                DecisionRow(
                    ts=r.get("ts") or utcnow(),
                    match_id=r.get("match_id"),
                    kind=r["kind"],
                    message=r["message"],
                    data=r.get("data") or {},
                )
                for r in records
            ])

    # -- replay / query helpers ------------------------------------------ #
    def match_ids(self) -> list[str]:
        with self.session() as s:
            # Deterministic order: DISTINCT alone is an accident of the backend's scan
            # (and differs between SQLite and Postgres), but replay iteration order
            # decides Kelly compounding, so runs must be reproducible.
            rows = s.execute(
                select(MatchSnapshotRow.match_id)
                .distinct()
                .order_by(MatchSnapshotRow.match_id)
            ).scalars().all()
            return list(rows)

    def latest_match_snapshot_meta(self) -> list[tuple[str, str, datetime]]:
        """``(match_id, period, ts)`` for the LAST snapshot of each match, read from the
        promoted columns only (no ``data`` blob). One indexed query via
        ``id IN (SELECT MAX(id) … GROUP BY match_id)`` instead of loading + validating every
        match's full snapshot history — lets a caller find the handful of live matches in ~ms
        regardless of table size. Ordered by match_id for deterministic iteration."""
        R = MatchSnapshotRow
        with self.session() as s:
            latest_ids = select(func.max(R.id)).group_by(R.match_id).scalar_subquery()
            rows = s.execute(
                select(R.match_id, R.period, R.ts)
                .where(R.id.in_(latest_ids))
                .order_by(R.match_id)
            ).all()
            return [(r.match_id, r.period, r.ts) for r in rows]

    def iter_match_snapshots(self, match_id: str) -> list[MatchSnapshot]:
        with self.session() as s:
            rows = (
                s.execute(
                    select(MatchSnapshotRow)
                    .where(MatchSnapshotRow.match_id == match_id)
                    .order_by(MatchSnapshotRow.ts, MatchSnapshotRow.id)
                )
                .scalars()
                .all()
            )
            return [MatchSnapshot.model_validate(r.data) for r in rows]

    def iter_market_snapshots(self, match_id: str) -> list[MarketSnapshot]:
        with self.session() as s:
            rows = (
                s.execute(
                    select(MarketSnapshotRow)
                    .where(MarketSnapshotRow.match_id == match_id)
                    .order_by(MarketSnapshotRow.ts, MarketSnapshotRow.id)
                )
                .scalars()
                .all()
            )
            return [MarketSnapshot.model_validate(r.data) for r in rows]

    def iter_edges(self, match_id: str) -> list[EdgeSignal]:
        with self.session() as s:
            rows = (
                s.execute(
                    select(EdgeRow)
                    .where(EdgeRow.match_id == match_id)
                    .order_by(EdgeRow.ts, EdgeRow.id)
                )
                .scalars()
                .all()
            )
            return [EdgeSignal.model_validate(r.data) for r in rows]
