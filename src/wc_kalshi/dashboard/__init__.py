"""FastAPI dashboard: live model-vs-market view, P&L, positions, kill switch."""

from .app import create_app

__all__ = ["create_app"]
