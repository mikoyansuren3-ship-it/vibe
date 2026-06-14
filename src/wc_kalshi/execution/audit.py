"""Audit trail.

Every signal, sizing decision, order, fill, guardrail trip, and alert is appended
to a JSONL file (one object per line) and mirrored into the ``decisions`` DB table,
so any trade can be fully explained after the fact from the snapshot that triggered
it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..logging_setup import get_logger
from ..util import utcnow

log = get_logger("audit")


class AuditLogger:
    def __init__(self, path: str | Path, db: Any | None = None, *, enabled: bool = True) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = db
        self.enabled = enabled  # backtests disable this for speed

    def log(self, kind: str, message: str, *, match_id: str | None = None, **data: Any) -> None:
        if not self.enabled:
            return
        record = {
            "ts": utcnow().isoformat(),
            "kind": kind,
            "match_id": match_id,
            "message": message,
            **data,
        }
        with self.path.open("a") as fh:
            fh.write(json.dumps(record, default=str, separators=(",", ":")) + "\n")
        if self.db is not None:
            try:
                self.db.record_decision(kind, message, match_id=match_id, data=data)
            except Exception as exc:  # never let auditing break the loop
                log.warning("audit db write failed", extra={"err": str(exc)})

    # Convenience wrappers -------------------------------------------------
    def signal(self, edge: Any) -> None:
        self.log(
            "signal",
            f"{edge.outcome.value} raw_edge={edge.raw_edge:+.3f} net={edge.net_edge:+.3f} "
            f"actionable={edge.actionable}",
            match_id=edge.match_id,
            outcome=edge.outcome.value,
            market_ticker=edge.market_ticker,
            model_prob=edge.model_prob,
            market_prob=edge.market_prob,
            raw_edge=edge.raw_edge,
            net_edge=edge.net_edge,
            actionable=edge.actionable,
        )

    def order(self, order: Any, result: Any) -> None:
        self.log(
            "order",
            f"{order.action.value} {order.contracts} {order.market_ticker} "
            f"@ {order.limit_price_cents}c -> {result.status.value}",
            match_id=order.match_id,
            client_order_id=order.client_order_id,
            exchange_order_id=result.exchange_order_id,
            action=order.action.value,
            contracts=order.contracts,
            price_cents=order.limit_price_cents,
            status=result.status.value,
            filled=result.filled_contracts,
            fee=result.fee,
        )

    def guardrail(self, reason: str, *, match_id: str | None = None, **data: Any) -> None:
        self.log("guardrail", reason, match_id=match_id, **data)

    def alert(self, kind: str, message: str, *, match_id: str | None = None, **data: Any) -> None:
        self.log(f"alert:{kind}", message, match_id=match_id, **data)
