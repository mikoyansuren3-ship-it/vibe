"""StatsBomb open-data converter — offline, against tiny captured-shape payloads."""

import json
from pathlib import Path

from wc_kalshi.backtest.historical import load_historical_match
from wc_kalshi.backtest.statsbomb import convert_match, coverage_report
from wc_kalshi.models.schemas import MatchPeriod, Outcome

DATA = Path(__file__).parent / "data"
META = json.loads((DATA / "statsbomb_match_meta.json").read_text())
EVENTS = json.loads((DATA / "statsbomb_events_sample.json").read_text())


def _tick(d, minute):
    return next(t for t in d["ticks"] if t["minute"] == minute)


def test_regulation_settlement_excludes_shootout_and_et():
    d = convert_match(META, EVENTS)
    ft = d["ticks"][-1]
    assert ft["period"] == "FT" and ft["minute"] == 90
    # Regulation only: Arg 2 (min23, min75), France 1 (P2 min45). The period-5 shootout
    # goal (France) and the ET in the official 3-3 meta result are both excluded.
    assert (ft["home_score"], ft["away_score"]) == (2, 1)
    assert ft["away_score"] != 2  # shootout goal must NOT count


def test_cumulative_xg_and_red_cards():
    d = convert_match(META, EVENTS)
    ft = d["ticks"][-1]
    assert ft["home_xg"] == 1.5  # 0.3 + 0.7 + 0.5
    assert ft["away_xg"] == 0.5  # 0.2 + 0.3  (shootout 0.8 excluded)
    assert ft["away_red"] == 1 and ft["home_red"] == 0


def test_half_aware_minute_mapping_no_collision():
    """A 1st-half-stoppage event (P1 min 47) and a 2nd-half-start event (P2 min 45) must
    both land at match-minute 45 — never leak to minute 47 as if second half."""
    d = convert_match(META, EVENTS)
    # At minute 44 neither the P1-stoppage shot nor the P2 goal is counted yet.
    assert _tick(d, 44)["away_xg"] == 0.0
    assert _tick(d, 44)["away_score"] == 0
    # At minute 45 BOTH France shots are counted: the P1-stoppage shot (raw min 47, xG 0.2)
    # is correctly mapped to match-minute 45 — not leaked to minute 47 as if 2nd half —
    # plus the P2 opening goal (xG 0.3). 0.2 + 0.3 = 0.5.
    assert _tick(d, 45)["away_xg"] == 0.5
    assert _tick(d, 45)["away_score"] == 1
    assert _tick(d, 45)["period"] == "2H"
    # Minutes 46/47 are unchanged — the P1-stoppage event did NOT create a phantom
    # second-half update at raw minute 47.
    assert _tick(d, 47)["away_xg"] == _tick(d, 45)["away_xg"]


def test_elo_and_context_filled():
    d = convert_match(META, EVENTS)
    assert d["home_elo"] == 2140 and d["away_elo"] == 2080  # Argentina / France
    assert d["neutral_venue"] is True
    assert d["metadata"]["elo_coverage"] == {"home": True, "away": True}
    assert d["metadata"]["stage"] == "Final"
    assert d["metadata"]["match_date"] == "2022-12-18"


def test_roundtrips_through_loader_and_settles_home():
    d = convert_match(META, EVENTS)
    ticks = load_historical_match(d)
    assert len(ticks) == 91
    final = ticks[-1][0]
    assert final.period is MatchPeriod.FULL_TIME
    assert final.score_diff == 1  # home win
    _ = Outcome.HOME


def test_knockout_tied_at_90_settles_draw():
    """ET winner is irrelevant to the 90′ market: a 1-1 at 90′ settles DRAW."""
    events = [
        {"index": 1, "type": {"name": "Shot"}, "period": 1, "minute": 30, "second": 0,
         "team": {"name": "Argentina"}, "shot": {"statsbomb_xg": 0.5, "outcome": {"name": "Goal"}}},
        {"index": 2, "type": {"name": "Shot"}, "period": 2, "minute": 80, "second": 0,
         "team": {"name": "France"}, "shot": {"statsbomb_xg": 0.6, "outcome": {"name": "Goal"}}},
        {"index": 3, "type": {"name": "Shot"}, "period": 4, "minute": 110, "second": 0,
         "team": {"name": "Argentina"}, "shot": {"statsbomb_xg": 0.4, "outcome": {"name": "Goal"}}},
    ]
    d = convert_match(META, events)
    ft = d["ticks"][-1]
    assert (ft["home_score"], ft["away_score"]) == (1, 1)  # ET goal excluded -> draw


def test_own_goal_credited_to_beneficiary():
    events = [
        {"index": 1, "type": {"name": "Shot"}, "period": 1, "minute": 20, "second": 0,
         "team": {"name": "France"}, "shot": {"statsbomb_xg": 0.4, "outcome": {"name": "Goal"}}},
        {"index": 2, "type": {"name": "Own Goal For"}, "period": 2, "minute": 50, "second": 0,
         "team": {"name": "Argentina"}},
    ]
    d = convert_match(META, events)
    ft = d["ticks"][-1]
    assert ft["home_score"] == 1  # Argentina credited via "Own Goal For"
    assert ft["away_score"] == 1
    assert ft["home_xg"] == 0.0  # own goals carry no xG


def test_match_without_xg_is_skipped():
    events = [
        {"index": 1, "type": {"name": "Pass"}, "period": 1, "minute": 5, "second": 0,
         "team": {"name": "Argentina"}},
    ]
    assert convert_match(META, events) is None


def test_coverage_report():
    d = convert_match(META, EVENTS)
    rep = coverage_report([d])
    assert rep["n_matches"] == 1 and rep["full_elo"] == 1 and rep["avg_ticks"] == 91.0
