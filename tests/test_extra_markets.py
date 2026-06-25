"""Broader per-match market capture (raw_market_quotes) — offline."""

from wc_kalshi.ingestion.kalshi.extra_markets import capture_extra_markets, quote_row
from wc_kalshi.util import utcnow


def test_quote_row_parses_dollar_schema_and_strike():
    market = {
        "ticker": "KXWCTOTAL-26JUN27JORARG-3",
        "yes_sub_title": "Over 2.5 goals scored",
        "floor_strike": 2.5,
        "strike_type": "greater",
        "yes_bid_dollars": "0.4600",
        "yes_ask_dollars": "0.4700",
        "last_price_dollars": "0.4700",
        "status": "active",
    }
    r = quote_row("SB-1", "KXWCTOTAL", market, utcnow())
    assert r["series"] == "KXWCTOTAL" and r["market_ticker"].endswith("-3")
    assert r["floor_strike"] == 2.5 and r["strike_type"] == "greater"
    assert r["yes_bid"] == 46 and r["yes_ask"] == 47 and r["last_price"] == 47


class _FakeClient:
    """Returns a 2-market Total event for the matching suffix, empty for others."""

    async def get_markets(self, *, event_ticker=None, **_):
        if event_ticker == "KXWCTOTAL-26JUN27JORARG":
            return {"markets": [
                {"ticker": "KXWCTOTAL-26JUN27JORARG-2", "yes_sub_title": "Over 1.5",
                 "floor_strike": 1.5, "strike_type": "greater", "yes_bid_dollars": "0.70",
                 "yes_ask_dollars": "0.72", "status": "active"},
                {"ticker": "KXWCTOTAL-26JUN27JORARG-3", "yes_sub_title": "Over 2.5",
                 "floor_strike": 2.5, "strike_type": "greater", "yes_bid_dollars": "0.45",
                 "yes_ask_dollars": "0.47", "status": "active"},
            ]}
        return {"markets": []}


async def test_capture_and_persist_round_trip(tmp_db):
    client = _FakeClient()
    rows = await capture_extra_markets(
        client, "SB-1", "26JUN27JORARG", series=("KXWCTOTAL", "KXWCSPREAD")
    )
    assert len(rows) == 2  # only KXWCTOTAL returned markets
    assert {r["series"] for r in rows} == {"KXWCTOTAL"}
    tmp_db.add_raw_market_quotes(rows)  # persists without error
