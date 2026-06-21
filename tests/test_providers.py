"""Provider mapping (offline) + Kalshi market mapping + orderbook parsing."""

from wc_kalshi.ingestion.football.apifootball import parse_period, snapshot_from_payload
from wc_kalshi.ingestion.football.simulated import SimulatedFootballProvider, simulate_full_match
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


def test_parse_period():
    assert parse_period("2H") is MatchPeriod.SECOND_HALF
    assert parse_period("FT") is MatchPeriod.FULL_TIME
    assert parse_period("NS") is MatchPeriod.PRE
    assert parse_period(None) is MatchPeriod.FIRST_HALF


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

    p = APIFootballProvider(api_key="x", fetch_statistics=False, fetch_context=False, league_id=1)
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

    p = APIFootballProvider(api_key="x", fetch_statistics=False, fetch_context=False, league_id=None)

    async def fake_get(endpoint, params):
        return _two_league_fixtures()

    monkeypatch.setattr(p, "_get", fake_get)
    snaps = await p.fetch_live()
    assert len(snaps) == 2
    await p.aclose()
