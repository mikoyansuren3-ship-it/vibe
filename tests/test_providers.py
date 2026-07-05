"""Provider mapping (offline) + Kalshi market mapping + orderbook parsing."""

import asyncio

from wc_kalshi.ingestion.football.apifootball import parse_period, snapshot_from_payload
from wc_kalshi.ingestion.football.simulated import SimulatedFootballProvider, simulate_full_match
from wc_kalshi.ingestion.football.thestatsapi import snapshot_from_match
from wc_kalshi.ingestion.kalshi.feed import market_snapshot_from_api, parse_orderbook
from wc_kalshi.ingestion.kalshi.market_map import match_event_to_markets
from wc_kalshi.models.schemas import MatchPeriod, Outcome


# -- API-Football mapping --------------------------------------------------- #
def test_apifootball_snapshot_mapping(sample_apifootball):
    snap = snapshot_from_payload(
        sample_apifootball, sample_apifootball["_statistics"], sample_apifootball["_events"]
    )
    assert snap.home_team == "USA" and snap.away_team == "Wales"
    assert snap.minute == 67 and snap.period is MatchPeriod.SECOND_HALF
    assert snap.home_score == 1 and snap.away_score == 0
    assert abs(snap.home.xg - 1.34) < 1e-6 and abs(snap.away.xg - 0.61) < 1e-6
    assert snap.home.shots == 11 and snap.home.shots_on_target == 4
    assert abs(snap.home.possession - 0.58) < 1e-6
    assert snap.away.red_cards == 1


def test_apifootball_lineup_and_injury_mapping(sample_apifootball):
    from wc_kalshi.ingestion.football.apifootball import apply_context, parse_injuries, parse_lineups

    lu = parse_lineups(sample_apifootball["_lineups"], "USA", "Wales")
    assert lu["home"]["formation"] == "4-3-3"
    assert "Pulisic" in lu["home"]["xi"] and "Turner" in lu["home"]["xi"]
    assert lu["away"]["formation"] == "3-4-3"

    inj = parse_injuries(sample_apifootball["_injuries"], "USA", "Wales")
    assert inj["home"] == ["McKennie"] and inj["away"] == ["Ramsey"]

    snap = snapshot_from_payload(sample_apifootball, None, None)
    apply_context(snap, lu, inj)
    assert snap.context.home_formation == "4-3-3"
    assert snap.context.home_injuries == ["McKennie"]
    assert "Bale" in snap.context.away_xi


def test_finished_match_settles_on_regulation_score():
    """A finished match must settle on the 90' score (score.fulltime), excluding ET/pens —
    Kalshi WC contracts resolve on 90'. Here the running 'goals' include an ET goal."""
    fixture = {
        "fixture": {"id": 5, "status": {"short": "AET", "elapsed": 120}},
        "teams": {"home": {"name": "Argentina"}, "away": {"name": "France"}},
        "goals": {"home": 3, "away": 3},  # after ET
        "score": {"fulltime": {"home": 2, "away": 2}},  # 90' regulation -> DRAW
    }
    snap = snapshot_from_payload(fixture)
    assert snap.period is MatchPeriod.FULL_TIME
    assert (snap.home_score, snap.away_score) == (2, 2)  # not 3-3


def test_interrupted_match_marked_abandoned():
    """An INTERRUPTED fixture (no valid 90' result) must be flagged 'abandoned', not '1H live'."""
    fixture = {
        "fixture": {"id": 9, "status": {"short": "INT", "elapsed": None}},
        "teams": {"home": {"name": "France"}, "away": {"name": "Iraq"}},
        "goals": {"home": 1, "away": 0},
        "score": {"fulltime": {"home": None, "away": None}},
    }
    snap = snapshot_from_payload(fixture)
    assert snap.status == "abandoned"


def test_parse_period():
    assert parse_period("2H") is MatchPeriod.SECOND_HALF
    assert parse_period("FT") is MatchPeriod.FULL_TIME
    assert parse_period("NS") is MatchPeriod.PRE
    assert parse_period(None) is MatchPeriod.FIRST_HALF


def test_xg_placeholder_strings_stay_none():
    """The API's blank-field placeholders ("", "-") must map to xg=None, not a fake
    "real xG = 0.0" that suppresses the shot proxy (the None contract in TeamStats)."""
    from wc_kalshi.ingestion.football.apifootball import team_stats_from_statistics

    def block(xg_value):
        return {"statistics": [{"type": "expected_goals", "value": xg_value}]}

    assert team_stats_from_statistics(block("")).xg is None
    assert team_stats_from_statistics(block("-")).xg is None
    assert team_stats_from_statistics(block(" - ")).xg is None
    assert team_stats_from_statistics(block(None)).xg is None
    assert team_stats_from_statistics(block("1.34")).xg == 1.34
    assert team_stats_from_statistics(block("0.0")).xg == 0.0  # explicit zero is real


def _cards_fixture(stats_reds: int, events: list) -> dict:
    return {
        "fixture": {"id": 7, "status": {"short": "2H", "elapsed": 70}},
        "teams": {"home": {"name": "USA"}, "away": {"name": "Wales"}},
        "goals": {"home": 0, "away": 0},
        "_stats": [{
            "team": {"name": "Wales"},
            "statistics": [{"type": "Red Cards", "value": stats_reds}],
        }],
        "_events": events,
    }


def _card(detail, team="Wales"):
    return {"type": "Card", "detail": detail, "team": {"name": team}}


def test_second_dismissal_counted_when_stats_lag():
    """Stats lag events by minutes; a second red (or second-yellow dismissal, whose
    detail has no 'red' in it) must still be counted — the old fallback capped at 1."""
    fx = _cards_fixture(1, [_card("Red Card"), _card("Second Yellow card")])
    snap = snapshot_from_payload(fx, fx["_stats"], fx["_events"])
    assert snap.away.red_cards == 2


def test_stats_red_count_kept_when_events_missing():
    fx = _cards_fixture(1, [])
    snap = snapshot_from_payload(fx, fx["_stats"], fx["_events"])
    assert snap.away.red_cards == 1  # max(stats, events) never loses the stats count


# -- TheStatsAPI xg None-contract -------------------------------------------- #
def test_thestatsapi_missing_xg_stays_none():
    """The fallback provider exists FOR xG; a failed /stats call must yield xg=None
    (proxy takes over), never a fabricated 0.0."""
    match = {"id": 1, "home_team": "USA", "away_team": "Wales", "minute": 70,
             "status": "live", "home_score": 0, "away_score": 0}
    snap = snapshot_from_match(match, stats=None)
    assert snap.home.xg is None and snap.away.xg is None

    with_xg = snapshot_from_match(match, stats={"home": {"xg": 2.1}, "away": {"xg": 0.0}})
    assert with_xg.home.xg == 2.1
    assert with_xg.away.xg == 0.0  # explicit zero from the provider is a real value


# -- Kalshi market mapping -------------------------------------------------- #
def test_market_map_resolves_three_outcomes(sample_kalshi_events):
    mp = match_event_to_markets(sample_kalshi_events, "m1", "USA", "Wales")
    assert mp is not None
    assert mp.event_ticker == "KXWCGAME-26JUN13USAWAL"
    assert mp.tickers[Outcome.HOME].endswith("USA")
    assert mp.tickers[Outcome.DRAW].endswith("DRAW")
    assert mp.tickers[Outcome.AWAY].endswith("WAL")


def test_market_map_returns_none_for_unknown_match(sample_kalshi_events):
    assert match_event_to_markets(sample_kalshi_events, "m1", "Brazil", "Japan") is None


# -- orderbook parsing ------------------------------------------------------ #
def test_parse_orderbook():
    ob = {"orderbook": {"yes": [[52, 100], [51, 200]], "no": [[45, 100], [44, 300]]}}
    yes_bid, yes_ask, yes_depth, no_depth = parse_orderbook(ob)
    assert yes_bid == 52  # best yes bid
    assert yes_ask == 55  # 100 - best no bid (45)
    assert yes_depth[0].price_cents == 52 and len(no_depth) == 2


def test_market_snapshot_prefers_book_when_market_obj_blank():
    market_obj = {"ticker": "t", "yes_bid": 0, "yes_ask": 0, "volume": 10, "status": "active"}
    ob = {"orderbook": {"yes": [[40, 10]], "no": [[55, 10]]}}
    snap = market_snapshot_from_api("m1", Outcome.HOME, market_obj, ob)
    assert snap.yes_bid == 40 and snap.yes_ask == 45


# -- simulated provider ----------------------------------------------------- #
async def test_simulated_provider_runs_to_completion():
    p = SimulatedFootballProvider(seed=1, num_matches=1, minutes_per_tick=15)
    seen = False
    for _ in range(20):
        snaps = await p.fetch_live()
        if snaps:
            seen = True
        if p.all_finished:
            break
    assert seen and p.all_finished


def test_simulate_full_match_finishes_with_xg():
    snaps = simulate_full_match(seed=5)
    assert snaps[-1].period is MatchPeriod.FULL_TIME
    assert len(snaps) >= 90
    assert snaps[-1].home.xg >= 0.0


def test_simulation_is_deterministic():
    a = simulate_full_match(seed=11)[-1]
    b = simulate_full_match(seed=11)[-1]
    assert (a.home_score, a.away_score, round(a.home.xg, 4)) == (b.home_score, b.away_score, round(b.home.xg, 4))


# -- API-Football league filter (offline; _get is monkeypatched) ------------ #
def _two_league_fixtures():
    def fx(fid, home, away, league_id):
        return {
            "fixture": {"id": fid, "status": {"short": "1H", "elapsed": 20}},
            "teams": {"home": {"name": home}, "away": {"name": away}},
            "goals": {"home": 0, "away": 0},
            "league": {"id": league_id, "name": "x"},
        }

    return {"response": [fx(1, "A", "B", 1), fx(2, "C", "D", 99)]}


async def test_apifootball_filters_to_league_client_side(monkeypatch):
    from wc_kalshi.ingestion.football.apifootball import APIFootballProvider

    p = APIFootballProvider(api_key="x", fetch_statistics=False, fetch_context=False,
                            fetch_events=False, league_id=1)
    captured: dict = {}

    async def fake_get(endpoint, params):
        captured.update(params=params)
        return _two_league_fixtures()

    monkeypatch.setattr(p, "_get", fake_get)
    snaps = await p.fetch_live()
    assert captured["params"] == {"live": "all"}  # always all; filter is client-side
    assert len(snaps) == 1 and snaps[0].home_team == "A"  # league 99 dropped
    await p.aclose()


async def test_apifootball_no_filter_keeps_all_leagues(monkeypatch):
    from wc_kalshi.ingestion.football.apifootball import APIFootballProvider

    p = APIFootballProvider(api_key="x", fetch_statistics=False, fetch_context=False,
                            fetch_events=False, league_id=None)

    async def fake_get(endpoint, params):
        return _two_league_fixtures()

    monkeypatch.setattr(p, "_get", fake_get)
    snaps = await p.fetch_live()
    assert len(snaps) == 2
    await p.aclose()


async def test_fetch_live_overlaps_per_fixture_requests(monkeypatch):
    """fetch_live must fan the per-fixture statistics/events fetches out concurrently, not
    run them 1+2N serially — otherwise the last live match's quote is N RTTs staler than
    the first. We hold each sub-request open and assert several were in flight at once."""
    from wc_kalshi.ingestion.football.apifootball import APIFootballProvider

    p = APIFootballProvider(api_key="x", fetch_context=False)  # statistics + events on
    inflight = 0
    max_inflight = 0

    async def fake_get(endpoint, params):
        nonlocal inflight, max_inflight
        if endpoint == "/fixtures":
            return _two_league_fixtures()  # two fixtures, no league filter
        inflight += 1
        max_inflight = max(max_inflight, inflight)
        await asyncio.sleep(0.02)  # hold the "connection" open to expose overlap
        inflight -= 1
        return {"response": []}

    monkeypatch.setattr(p, "_get", fake_get)
    snaps = await p.fetch_live()
    assert len(snaps) == 2
    # 2 fixtures x (stats + events) = 4 sub-requests; >=3 concurrent proves BOTH the
    # within-fixture gather and the cross-fixture fan-out (a single fixture tops out at 2).
    assert max_inflight >= 3
    await p.aclose()


# -- upcoming-fixture projection -------------------------------------------- #
async def test_sim_fetch_upcoming_projects_pre_games():
    p = SimulatedFootballProvider(seed=3, num_matches=2)
    snaps = await p.fetch_upcoming(limit=5)
    assert snaps  # surfaces the fixtures after the live window
    assert all(s.period is MatchPeriod.PRE and s.status == "scheduled" for s in snaps)
    assert all(s.match_id.startswith("sim-up-") for s in snaps)
    assert all(s.context and s.context.kickoff is not None for s in snaps)
    kickoffs = [s.context.kickoff for s in snaps]
    assert kickoffs == sorted(kickoffs)  # staggered, ascending


def test_apifootball_upcoming_from_payload():
    from wc_kalshi.ingestion.football.apifootball import upcoming_from_payload

    fixture = {
        "fixture": {"id": 42, "date": "2026-06-30T18:00:00+00:00",
                    "status": {"short": "NS", "elapsed": None}, "venue": {"name": "Stadium"}},
        "teams": {"home": {"name": "Spain"}, "away": {"name": "Germany"}},
        "goals": {"home": None, "away": None},
        "league": {"id": 1, "name": "World Cup"},
    }
    snap = upcoming_from_payload(fixture)
    assert snap.period is MatchPeriod.PRE and snap.status == "scheduled"
    assert snap.minute == 0 and snap.home_score == 0 and snap.away_score == 0
    assert snap.context and snap.context.kickoff is not None
    assert snap.context.kickoff.isoformat() == "2026-06-30T18:00:00+00:00"
    assert snap.context.home_elo is not None  # Elo priors injected via apply_ratings


async def test_apifootball_fetch_upcoming_filters_league(monkeypatch):
    from wc_kalshi.ingestion.football.apifootball import APIFootballProvider

    p = APIFootballProvider(api_key="x", fetch_statistics=False, fetch_context=False,
                            fetch_events=False, league_id=1)
    captured: dict = {}

    def nsfx(fid, home, away, league_id):
        return {
            "fixture": {"id": fid, "date": "2026-07-01T15:00:00+00:00", "status": {"short": "NS"}},
            "teams": {"home": {"name": home}, "away": {"name": away}},
            "goals": {"home": None, "away": None}, "league": {"id": league_id, "name": "x"},
        }

    async def fake_get(endpoint, params):
        captured.update(endpoint=endpoint, params=params)
        return {"response": [nsfx(1, "A", "B", 1), nsfx(2, "C", "D", 99)]}

    monkeypatch.setattr(p, "_get", fake_get)
    snaps = await p.fetch_upcoming(limit=4)
    assert captured["endpoint"] == "/fixtures" and captured["params"]["next"] == "4"
    assert len(snaps) == 1 and snaps[0].home_team == "A"  # league 99 dropped
    assert all(s.period is MatchPeriod.PRE for s in snaps)
    await p.aclose()


# -- stage awareness (knockout vs group) ------------------------------------ #
def test_is_knockout_round_truth_table():
    from wc_kalshi.modeling.ratings import is_knockout_round

    for label in ("Round of 16", "Round of 32", "Quarter-finals", "Semi-finals", "Final",
                  "3rd Place Final", "Knockout Round", "Play-offs", "1/8-finals"):
        assert is_knockout_round(label), label
    for label in ("Group A", "Group Stage", "Regular Season - 1", "Matchday 3", "", None):
        assert not is_knockout_round(label), label


async def test_sim_fetch_upcoming_includes_knockout():
    p = SimulatedFootballProvider(seed=3, num_matches=2)
    snaps = await p.fetch_upcoming(limit=8)
    ko = [s for s in snaps if s.context and s.context.is_knockout]
    assert ko  # the demo surfaces knockout fixtures
    assert all(s.context.round == "Round of 16" for s in ko)
    assert all(s.match_id.startswith("sim-up-ko-") for s in ko)
    # group-stage upcoming fixtures remain non-knockout.
    assert any(s.context and not s.context.is_knockout for s in snaps)


def test_apifootball_upcoming_marks_knockout_round():
    from wc_kalshi.ingestion.football.apifootball import upcoming_from_payload

    fixture = {
        "fixture": {"id": 9, "date": "2026-07-05T18:00:00+00:00", "status": {"short": "NS"}},
        "teams": {"home": {"name": "Brazil"}, "away": {"name": "Japan"}},
        "goals": {"home": None, "away": None},
        "league": {"id": 1, "name": "World Cup", "round": "Round of 16"},
    }
    snap = upcoming_from_payload(fixture)
    assert snap.context.round == "Round of 16" and snap.context.is_knockout is True
