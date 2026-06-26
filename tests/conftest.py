"""Shared pytest fixtures. All tests run fully offline (no network, no keys)."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

from wc_kalshi.config import load_config
from wc_kalshi.models.db import Database
from wc_kalshi.models.schemas import (
    MatchContext,
    MatchPeriod,
    MatchSnapshot,
    TeamStats,
)

DATA_DIR = Path(__file__).parent / "data"


@pytest.fixture
def cfg():
    # load_env=False + use_local=False so a developer's real .env / config/local.yaml
    # (fees, providers, capture flags) never leak into tests — keep them deterministic.
    return load_config(load_env=False, use_local=False)


@pytest.fixture
def model_cfg(cfg):
    return cfg.model


@pytest.fixture
def tmp_db():
    # mkstemp (not the deprecated, race-prone mktemp) creates the file safely.
    fd, path = tempfile.mkstemp(prefix="wck-test-", suffix=".sqlite3")
    os.close(fd)
    return Database(f"sqlite:///{path}")


@pytest.fixture
def sample_apifootball():
    return json.loads((DATA_DIR / "apifootball_fixture.json").read_text())


@pytest.fixture
def sample_kalshi_events():
    return json.loads((DATA_DIR / "kalshi_events.json").read_text())


def make_match(
    *,
    match_id="m1",
    minute=0,
    period=MatchPeriod.FIRST_HALF,
    home_score=0,
    away_score=0,
    home_xg=0.0,
    away_xg=0.0,
    home_red=0,
    away_red=0,
    home_elo=1800.0,
    away_elo=1800.0,
    neutral=True,
    status="live",
) -> MatchSnapshot:
    """Convenience builder for a MatchSnapshot in tests."""
    return MatchSnapshot(
        match_id=match_id,
        provider="test",
        home_team="Home",
        away_team="Away",
        minute=minute,
        period=period,
        home_score=home_score,
        away_score=away_score,
        home=TeamStats(xg=home_xg, red_cards=home_red),
        away=TeamStats(xg=away_xg, red_cards=away_red),
        status=status,
        context=MatchContext(
            neutral_venue=neutral, home_elo=home_elo, away_elo=away_elo
        ),
    )


@pytest.fixture
def match_factory():
    return make_match
