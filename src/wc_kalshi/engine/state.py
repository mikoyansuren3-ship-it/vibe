"""In-memory runtime state the dashboard reads (live view of every active match)."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any

from ..util import utcnow


@dataclass
class RuntimeState:
    mode: str = "paper"
    started_at: str = field(default_factory=lambda: utcnow().isoformat())
    matches: dict[str, dict[str, Any]] = field(default_factory=dict)
    risk: dict[str, Any] = field(default_factory=dict)
    portfolio: dict[str, Any] = field(default_factory=dict)
    recent_decisions: deque = field(default_factory=lambda: deque(maxlen=100))

    def update_match(self, match_id: str, data: dict[str, Any]) -> None:
        self.matches[match_id] = {**self.matches.get(match_id, {}), **data, "updated": utcnow().isoformat()}

    def add_decision(self, decision: dict[str, Any]) -> None:
        self.recent_decisions.appendleft({**decision, "ts": utcnow().isoformat()})

    def to_json(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "started_at": self.started_at,
            "now": utcnow().isoformat(),
            "matches": list(self.matches.values()),
            "risk": self.risk,
            "portfolio": self.portfolio,
            "recent_decisions": list(self.recent_decisions)[:40],
        }
