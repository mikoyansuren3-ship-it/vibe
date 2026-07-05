"""Betfair historical parser + HT-aware timeline + merge — offline."""

import json
from pathlib import Path

from wc_kalshi.backtest.betfair import merge_markets, parse_stream_path
from wc_kalshi.backtest.statsbomb import convert_match

DATA = Path(__file__).parent / "data"
STREAM = DATA / "betfair_stream_sample.ndjson"
META = json.loads((DATA / "statsbomb_match_meta.json").read_text())
EVENTS = json.loads((DATA / "statsbomb_events_sample.json").read_text())


def test_parse_runner_map_and_clock_mode():
    (tl,) = parse_stream_path(STREAM)
    assert tl.market_id == "1.111"
    assert tl.runners == {1: "Argentina", 2: "The Draw", 3: "France"}
    # inPlay flips true->false->true around half-time => detectable.
    assert tl.clock_mode == "ht_detected"
    assert tl.t0_inplay_pt == 1000000


def test_preoff_captured_before_inplay():
    (tl,) = parse_stream_path(STREAM)
    assert tl.pre_off  # the rc at pt 970000 (< t0) is the pre-off line
    pre, _inplay = tl.as_outcomes("Argentina", "France")
    assert set(pre) == {"home", "draw", "away"}
    # Argentina @2.2 decimal -> ~0.45 implied -> ~45c yes.
    assert 40 <= pre["home"][0] <= 50


def test_ht_aware_minute_alignment():
    (tl,) = parse_stream_path(STREAM)
    _pre, inplay = tl.as_outcomes("Argentina", "France")
    # first-half updates map by raw elapsed; second-half updates subtract the HT gap.
    # pt 1.6e6 -> min 10, pt 3.4e6 -> min 40, pt 4.84e6 (after ht_end 4.72e6) -> 45+2=47,
    # pt 6.7e6 -> 45+33=78.
    assert sorted(inplay) == [10, 40, 47, 78]


def test_atb_atl_ladder_used_when_present():
    (tl,) = parse_stream_path(STREAM)
    _pre, inplay = tl.as_outcomes("Argentina", "France")
    bid, ask = inplay[10]["home"]  # built from atb/atl at pt 1.6e6
    assert 1 <= bid < ask <= 99


def test_merge_injects_quotes_into_statsbomb_match():
    match = convert_match(META, EVENTS)
    timelines = parse_stream_path(STREAM)
    merged, report = merge_markets([match], timelines)
    assert report.matched == 1
    assert report.clock_modes == {"ht_detected": 1}
    # pre-off lands on the earliest tick (the CLV pre-off reference).
    assert "markets" in match["ticks"][0]
    assert match["metadata"]["betfair_market_id"] == "1.111"
    assert match["metadata"]["price_source"] == "betfair"
    with_mk = [t for t in match["ticks"] if "markets" in t]
    assert len(with_mk) > 1  # pre-off + several in-play ticks


def test_merge_rejects_date_mismatch():
    match = convert_match(META, EVENTS)
    match["metadata"]["match_date"] = "2021-01-01"  # >1 day from the Betfair marketTime
    _merged, report = merge_markets([match], parse_stream_path(STREAM))
    assert report.matched == 0
    assert match["match_id"] in report.unmatched_statsbomb


def test_merge_never_attaches_future_quotes():
    """Carry-forward-only: a tick must never receive a quote captured at a LATER
    minute — a post-goal price attached to a pre-goal tick is lookahead in the
    edge-vs-Betfair measurement."""
    match = {
        "match_id": "m-look",
        "home_team": "Argentina",
        "away_team": "France",
        "metadata": {"match_date": "2022-12-18"},
        "ticks": [
            {"minute": 9, "period": "1H"},   # only future quote (10') in range -> none
            {"minute": 11, "period": "1H"},  # past quote at 10' -> attaches
            {"minute": 14, "period": "1H"},  # nearest is 15' (future); 10' too old -> none
        ],
    }

    from datetime import datetime, timezone

    class _TL:
        market_id = "1.999"
        market_time = datetime(2022, 12, 18, 15, 0, tzinfo=timezone.utc)
        clock_mode = "ht_detected"

        @staticmethod
        def as_outcomes(home, away):
            return {}, {10: {"home": 0.40}, 15: {"home": 0.90}}

    merged, report = merge_markets([match], [_TL()])
    ticks = merged[0]["ticks"]
    assert "markets" not in ticks[0]  # 10' quote is in the tick's future
    assert ticks[1]["markets"] == {"home": 0.40}  # carried forward from 10'
    assert "markets" not in ticks[2]  # 15' is future; 10' is 4' old (> tolerance)


def test_non_match_odds_markets_are_filtered_before_accumulating(tmp_path):
    """Only MATCH_ODDS markets become timelines; other market types are dropped at ingest
    (not buffered then discarded), so a PRO archive doesn't balloon RAM."""
    extra = json.dumps({"pt": 970000, "mc": [{
        "id": "9.999",
        "marketDefinition": {"marketType": "CORRECT_SCORE"},
        "rc": [{"id": 1, "ltp": 5.0}],
    }]})
    mixed = tmp_path / "mixed.ndjson"
    mixed.write_text(STREAM.read_text().rstrip() + "\n" + extra + "\n")
    ids = {tl.market_id for tl in parse_stream_path(mixed)}
    assert ids == {"1.111"}  # the CORRECT_SCORE market never becomes a timeline


def test_bz2_stream_parses_identically_to_plain(tmp_path):
    """The streaming .bz2 path (no whole-file read into RAM) yields the same parse as plain."""
    import bz2

    comp = tmp_path / "s.ndjson.bz2"
    comp.write_bytes(bz2.compress(STREAM.read_bytes()))
    (plain,) = parse_stream_path(STREAM)
    (streamed,) = parse_stream_path(comp)
    assert streamed.market_id == plain.market_id
    assert streamed.runners == plain.runners
    assert streamed.clock_mode == plain.clock_mode
