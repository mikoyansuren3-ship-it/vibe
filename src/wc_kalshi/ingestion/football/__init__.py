"""Live football-data providers behind one interface."""

from .base import FootballDataProvider, build_football_provider

__all__ = ["FootballDataProvider", "build_football_provider"]
