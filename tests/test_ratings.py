"""National-team rating priors injected into the live model."""

from wc_kalshi.ingestion.football.apifootball import snapshot_from_payload
from wc_kalshi.ingestion.football.thestatsapi import snapshot_from_match
from wc_kalshi.modeling.ratings import (
    apply_ratings,
    canonical_team,
    elo_for,
    infer_neutral_venue,
)
from wc_kalshi.models.schemas import MatchContext


def test_canonical_aliases():
    assert canonical_team("United States") == "USA"
    assert canonical_team("Korea Republic") == "South Korea"
    assert canonical_team("Czech Republic") == "Czechia"
    assert canonical_team("Nowhere FC") is None


def test_elo_lookup():
    assert elo_for("Argentina") > elo_for("Canada")
    assert elo_for("totally unknown") is None


def test_host_nation_is_not_neutral():
    assert infer_neutral_venue("USA") is False  # host plays at home
    assert infer_neutral_venue("Brazil") is True  # neutral WC venue


def test_apply_ratings_only_fills_missing():
    ctx = MatchContext(home_elo=1234.0)  # explicit value must win
    out = apply_ratings(ctx, "Argentina", "France")
    assert out.home_elo == 1234.0  # not overwritten
    assert out.away_elo == elo_for("France")


def test_apifootball_snapshot_has_real_priors(sample_apifootball):
    snap = snapshot_from_payload(sample_apifootball)
    assert snap.context.home_elo is not None  # was None (flat constant) before
    assert snap.context.away_elo is not None


def test_thestatsapi_snapshot_has_real_priors():
    snap = snapshot_from_match(
        {"id": "x", "home_team": "Brazil", "away_team": "Japan", "minute": 10, "status": "1H"}
    )
    assert snap.context.home_elo == elo_for("Brazil")
    assert snap.context.neutral_venue is True  # Brazil not a host
