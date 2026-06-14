"""Small shared helpers."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone


def utcnow() -> datetime:
    """Timezone-aware current UTC time. Everything in the system is UTC."""
    return datetime.now(tz=timezone.utc)


def new_id(prefix: str = "") -> str:
    """Short unique id, optionally prefixed (used for client order ids etc.)."""
    token = uuid.uuid4().hex[:16]
    return f"{prefix}{token}" if prefix else token


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))
