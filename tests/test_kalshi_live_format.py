"""Regression tests for the live Kalshi 2026 WC integration (verified vs prod 2026-06-20).

Two real-world format issues these lock in:
  * the API moved to a dollar / fixed-point schema (``yes_bid_dollars`` = "0.2900",
    ``orderbook_fp`` with ``yes_dollars``/``no_dollars`` levels), and
  * WC match markets ship under series ``KXWCGAME`` with a market ``title`` naming BOTH
    teams ("Jordan vs Argentina Winner?"), so outcomes must be classified on the
    Yes-specific label, not the title.
"""

from wc_kalshi.ingestion.kalshi.feed import market_snapshot_from_api, parse_orderbook
from wc_kalshi.ingestion.kalshi.market_map import match_event_to_markets
from wc_kalshi.models.schemas import Outcome

# A real-shaped KXWCGAME events payload (prices in the dollar schema).
WC_EVENTS = {
    "events": [
        {
            "event_ticker": "KXWCGAME-26JUN27JORARG",
            "title": "Jordan vs Argentina",
            "sub_title": "JOR vs ARG (Jun 27)",
            "markets": [
                {"ticker": "KXWCGAME-26JUN27JORARG-JOR", "yes_sub_title": "Jordan",
                 "subtitle": "Jordan", "title": "Jordan vs Argentina Winner?"},
                {"ticker": "KXWCGAME-26JUN27JORARG-ARG", "yes_sub_title": "Argentina",
                 "subtitle": "Argentina", "title": "Jordan vs Argentina Winner?"},
                {"ticker": "KXWCGAME-26JUN27JORARG-TIE", "yes_sub_title": "Tie",
                 "subtitle": "Tie", "title": "Jordan vs Argentina Winner?"},
            ],
        }
    ]
}


def test_classifies_outcomes_despite_both_teams_in_title():
    mp = match_event_to_markets(WC_EVENTS, "m", home_team="Argentina", away_team="Jordan")
    assert mp is not None
    got = {o.value: tk.rsplit("-", 1)[-1] for o, tk in mp.tickers.items()}
    assert got == {"home": "ARG", "away": "JOR", "draw": "TIE"}


def test_mapping_is_orientation_correct():
    mp = match_event_to_markets(WC_EVENTS, "m", home_team="Jordan", away_team="Argentina")
    got = {o.value: tk.rsplit("-", 1)[-1] for o, tk in mp.tickers.items()}
    assert got == {"home": "JOR", "away": "ARG", "draw": "TIE"}


def test_market_object_dollar_schema():
    mkt = {
        "ticker": "KXWCGAME-26JUN27CODUZB-COD",
        "yes_bid_dollars": "0.4600", "yes_ask_dollars": "0.4700",
        "last_price_dollars": "0.4700", "volume_fp": "1234.0", "open_interest_fp": "999.0",
    }
    snap = market_snapshot_from_api("m", Outcome.HOME, mkt)
    assert snap.yes_bid == 46 and snap.yes_ask == 47 and snap.last_price == 47
    assert snap.volume == 1234 and snap.open_interest == 999


def test_orderbook_fp_dollar_levels():
    ob = {"orderbook_fp": {
        "yes_dollars": [["0.4600", "2117.0"], ["0.4500", "4946.0"]],
        "no_dollars": [["0.5300", "2239.0"], ["0.5200", "20835.0"]],
    }}
    yes_bid, yes_ask, yes_depth, no_depth = parse_orderbook(ob)
    assert yes_bid == 46           # best yes bid
    assert yes_ask == 100 - 53     # 100 - best no price
    assert yes_depth[0].price_cents == 46 and yes_depth[0].size == 2117


def test_legacy_cents_schema_still_parses():
    mkt = {"ticker": "X", "yes_bid": 45, "yes_ask": 47, "last_price": 46}
    ob = {"orderbook": {"yes": [[45, 100]], "no": [[52, 80]]}}
    snap = market_snapshot_from_api("m", Outcome.HOME, mkt, ob)
    assert snap.yes_bid == 45 and snap.yes_ask == 47
    assert snap.yes_depth[0].price_cents == 45 and snap.yes_depth[0].size == 100
