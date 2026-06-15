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

from sqlalchemy import JSON, Boolean, DateTime, Float, Integer, String, create_engine, select
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


class Database:
    """Thin wrapper around the engine + session factory with typed inserts."""

    def __init__(self, db_url: str, *, echo: bool = False) -> None:
        # Defensively ensure a sqlite file's parent directory exists.
        if db_url.startswith("sqlite:///") and ":memory:" not in db_url:
            from pathlib import Path

            Path(db_url[len("sqlite:///"):]).parent.mkdir(parents=True, exist_ok=True)
        connect_args = {"check_same_thread": False} if db_url.startswith("sqlite") else {}
        self.engine = create_engine(db_url, echo=echo, future=True, connect_args=connect_args)
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

    def add_market_snapshot(self, snap: MarketSnapshot) -> None:
        with self.session() as s:
            s.add(
                MarketSnapshotRow(
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
            )

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

    def add_edge(self, edge: EdgeSignal) -> None:
        with self.session() as s:
            s.add(
                EdgeRow(
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
            )

    def record_decision(
        self,
        kind: str,
        message: str,
        *,
        match_id: str | None = None,
        data: dict[str, Any] | None = None,
        ts: datetime | None = None,
    ) -> None:
        with self.session() as s:
            s.add(
                DecisionRow(
                    ts=ts or utcnow(),
                    match_id=match_id,
                    kind=kind,
                    message=message,
                    data=data or {},
                )
            )

    # -- replay / query helpers ------------------------------------------ #
    def match_ids(self) -> list[str]:
        with self.session() as s:
            rows = s.execute(select(MatchSnapshotRow.match_id).distinct()).scalars().all()
            return list(rows)

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
