"""Betfair historical exchange data -> market quotes for the historical backtest.

This attaches **real** MATCH_ODDS prices to the StatsBomb xG ticks so ``wck historical``
can report an edge against a real line instead of a synthetic one.

Honesty caveats (documented; non-negotiable):
  * Betfair is an **exchange** (the closest analogue to Kalshi) but it is **not Kalshi** —
    report the result as "edge vs the Betfair line", never "Kalshi CLV".
  * In-play CLV vs the *last* tick is degenerate near full time; the primary signal is
    the **pre-off line** (the last quote before the market turned in-play), which this
    module captures explicitly. See ``replay.py`` CLV references.
  * In-play clock alignment is **best-effort**: Betfair soccer keeps ``inPlay=true`` across
    half-time (HT shows as ``status: SUSPENDED``), so we detect HT from several signals and
    fall back to a fixed +15′ offset, flagging the mode. The pre-off metric is unaffected.
  * **Advanced/PRO** historical is required for in-play ``ltp``/ladders; the free BASIC tier
    is pre-off only. Files are **user-supplied** (licensing/ToS) — we parse, never redistribute.

Input: Betfair historical stream files (NDJSON ``mcm`` messages), optionally ``.bz2`` or
packed in a ``.tar``. Pass a single file or a directory (parsed recursively).
"""

from __future__ import annotations

import bz2
import io
import json
import tarfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from ..logging_setup import get_logger
from ..modeling.ratings import canonical_team

log = get_logger("betfair")

_DRAW_NAMES = frozenset({"the draw", "draw", "x"})


def _decimal_to_prob(odds: float | None) -> float | None:
    if not odds or odds <= 1.0:
        return None
    return 1.0 / float(odds)


def _quote_cents(mid_prob: float, half_spread: float) -> tuple[int, int]:
    """A symmetric Yes book in cents around a real traded mid probability."""
    bid = round((mid_prob - half_spread) * 100)
    ask = round((mid_prob + half_spread) * 100)
    bid = max(1, min(99, bid))
    ask = max(bid + 1, min(99, ask))
    return bid, ask


@dataclass
class MarketTimeline:
    """One Betfair MATCH_ODDS market: runner identities + pre-off and in-play quotes."""

    market_id: str
    event_name: str
    market_time: datetime | None
    runners: dict[int, str]  # selectionId -> runner name (from marketDefinition)
    t0_inplay_pt: int | None = None
    clock_mode: str = "preoff_only"  # ht_detected | offset_fallback | preoff_only
    # quotes keyed by runner name -> (yes_bid, yes_ask) cents:
    pre_off: dict[str, tuple[int, int]] = field(default_factory=dict)
    inplay: dict[int, dict[str, tuple[int, int]]] = field(default_factory=dict)  # minute->name->q

    def _outcome_map(self, home: str, away: str) -> dict[str, str]:
        """runner name -> 'home'/'draw'/'away' for this fixture (canonicalised)."""
        home_c, away_c = canonical_team(home), canonical_team(away)
        out: dict[str, str] = {}
        for name in self.runners.values():
            low = (name or "").strip().lower()
            nc = canonical_team(name)
            if low in _DRAW_NAMES:
                out[name] = "draw"
            elif (nc is not None and nc == home_c) or name == home:
                out[name] = "home"
            elif (nc is not None and nc == away_c) or name == away:
                out[name] = "away"
        return out

    def as_outcomes(
        self, home: str, away: str
    ) -> tuple[dict[str, list[int]] | None, dict[int, dict[str, list[int]]]]:
        """Re-key pre-off + in-play quotes from runner names to home/draw/away.

        Returns ``(pre_off, {minute: {outcome: [bid, ask]}})`` in the ``historical.py``
        ``markets`` shape; ``None`` pre-off if the runners could not be mapped.
        """
        omap = self._outcome_map(home, away)
        if len(set(omap.values())) < 2:  # could not resolve the fixture's runners
            return None, {}
        pre = {omap[n]: list(q) for n, q in self.pre_off.items() if n in omap} or None
        inplay: dict[int, dict[str, list[int]]] = {}
        for minute, byname in self.inplay.items():
            row = {omap[n]: list(q) for n, q in byname.items() if n in omap}
            if row:
                inplay[minute] = row
        return pre, inplay


@dataclass
class MergeReport:
    matched: int = 0
    unmatched_statsbomb: list[str] = field(default_factory=list)
    unmatched_betfair: list[str] = field(default_factory=list)
    clock_modes: dict[str, int] = field(default_factory=dict)

    def summary(self) -> str:
        return (
            f"merged {self.matched} match(es); "
            f"{len(self.unmatched_statsbomb)} StatsBomb + {len(self.unmatched_betfair)} "
            f"Betfair markets unmatched; clock modes: {self.clock_modes}"
        )


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def _iter_text_streams(path: str | Path) -> Iterator[tuple[str, io.TextIOBase]]:
    """Yield (name, text-stream) for every NDJSON source under ``path``.

    Handles a single ``.json``/``.ndjson``/``.bz2`` file, a ``.tar`` of ``.bz2`` files
    (Betfair PRO packaging), or a directory searched recursively.
    """
    p = Path(path)
    if p.is_dir():
        for child in sorted(p.rglob("*")):
            if child.is_file() and child.suffix in {".bz2", ".json", ".ndjson", ".tar"}:
                yield from _iter_text_streams(child)
        return
    if p.suffix == ".tar" or p.name.endswith(".tar.bz2"):
        with tarfile.open(p, "r:*") as tf:
            for member in tf.getmembers():
                if not member.isfile():
                    continue
                fh = tf.extractfile(member)
                if fh is None:
                    continue
                data = fh.read()
                if member.name.endswith(".bz2"):
                    data = bz2.decompress(data)
                yield member.name, io.StringIO(data.decode("utf-8", "replace"))
        return
    if p.suffix == ".bz2":
        with bz2.open(p, "rt", encoding="utf-8") as fh:
            yield p.name, io.StringIO(fh.read())
        return
    with open(p, encoding="utf-8") as fh:
        yield p.name, io.StringIO(fh.read())


def _detect_ht(
    defs: list[tuple[int, bool, str]], inplay_pts: list[int], t0: int
) -> tuple[int | None, int | None, str]:
    """Best-effort half-time detection. Returns (ht_start_pt, ht_end_pt, clock_mode).

    Priority: (1) an inPlay True->False->True flip; (2) a sustained SUSPENDED stretch or a
    large update gap in the [35, 70]-minute window; (3) fixed +15′ offset fallback.
    """
    # (1) inPlay flips back to false then true again.
    seen_inplay = False
    ht_start = ht_end = None
    for pt, inplay, _status in defs:
        if pt < t0:
            continue
        if inplay:
            if seen_inplay and ht_start is not None and ht_end is None:
                ht_end = pt
                return ht_start, ht_end, "ht_detected"
            seen_inplay = True
        elif seen_inplay and ht_start is None:
            ht_start = pt
    # (2) largest gap between in-play updates inside a plausible HT window.
    best_gap, gs, ge = 0, None, None
    for a, b in zip(inplay_pts, inplay_pts[1:]):
        elapsed_min = (a - t0) / 60000.0
        if 35 <= elapsed_min <= 70 and (b - a) > best_gap:
            best_gap, gs, ge = b - a, a, b
    if best_gap > 5 * 60000:  # > 5 real minutes of silence mid-game ~= the HT break
        return gs, ge, "ht_detected"
    return None, None, "offset_fallback"


def _build_timeline(market_id: str, msgs: list[tuple[int, dict[str, Any]]]) -> MarketTimeline:
    """Assemble one market's timeline from its time-ordered (pt, mc) messages.

    ``half_spread`` defaults to ~1c each side around the traded mid; widened to the real
    back/lay gap when ladders (``atb``/``atl``) are present.
    """
    runners: dict[int, str] = {}
    event_name = ""
    market_time: datetime | None = None
    defs: list[tuple[int, bool, str]] = []  # (pt, inPlay, status) from marketDefinitions
    rc_by_pt: list[tuple[int, dict[int, dict[str, Any]]]] = []  # (pt, {sel_id: rc})

    for pt, mc in msgs:
        mdef = mc.get("marketDefinition")
        if mdef:
            for r in mdef.get("runners", []):
                if r.get("id") is not None and r.get("name"):
                    runners[int(r["id"])] = str(r["name"])
            event_name = mdef.get("eventName", event_name)
            mt = mdef.get("marketTime")
            if mt and market_time is None:
                try:
                    market_time = datetime.fromisoformat(str(mt).replace("Z", "+00:00"))
                except ValueError:
                    pass
            defs.append((pt, bool(mdef.get("inPlay")), str(mdef.get("status", ""))))
        rc = mc.get("rc")
        if rc:
            rc_by_pt.append((pt, {int(r["id"]): r for r in rc if r.get("id") is not None}))

    tl = MarketTimeline(market_id, event_name, market_time, runners)

    # latest quote per runner, replayed in publish-time order.
    latest: dict[int, tuple[int, int]] = {}
    t0 = next((pt for pt, ip, _ in defs if ip), None)
    tl.t0_inplay_pt = t0
    inplay_pts = [pt for pt, rc in rc_by_pt if t0 is not None and pt >= t0]

    if t0 is not None:
        ht_start, ht_end, mode = _detect_ht(defs, inplay_pts, t0)
        tl.clock_mode = mode
    else:
        ht_start = ht_end = None

    def match_minute(pt: int) -> int | None:
        if t0 is None or pt < t0:
            return None
        if ht_end is not None and ht_start is not None:
            if pt < ht_start:
                m = (pt - t0) / 60000.0
            elif pt < ht_end:
                m = 45.0
            else:
                m = 45.0 + (pt - ht_end) / 60000.0
        else:  # offset fallback: assume the HT break sits at real-elapsed 45..60
            elapsed = (pt - t0) / 60000.0
            m = elapsed if elapsed <= 45 else (45.0 if elapsed < 60 else elapsed - 15)
        return max(0, min(90, round(m)))

    pre_off_recorded = False
    for pt, rc in rc_by_pt:
        for sel_id, change in rc.items():
            q = _runner_quote(change)
            if q is not None:
                latest[sel_id] = q
        snapshot = {runners[s]: q for s, q in latest.items() if s in runners}
        if not snapshot:
            continue
        if t0 is None or pt < t0:
            tl.pre_off = dict(snapshot)  # keep advancing to the LAST pre-off snapshot
            pre_off_recorded = True
        else:
            minute = match_minute(pt)
            if minute is not None:
                tl.inplay[minute] = dict(snapshot)
    if not pre_off_recorded and tl.inplay:
        # BASIC tier / no pre-off rows: fall back to the earliest in-play quote.
        tl.pre_off = dict(tl.inplay[min(tl.inplay)])
    return tl


def _runner_quote(change: dict[str, Any], default_half_spread: float = 0.01) -> tuple[int, int] | None:
    """Map one runner change to (yes_bid, yes_ask) cents around its traded mid."""
    atb = change.get("atb")  # available to back [[price,size],...]
    atl = change.get("atl")  # available to lay
    back = atb[0][0] if atb else None
    lay = atl[0][0] if atl else None
    p_back, p_lay = _decimal_to_prob(back), _decimal_to_prob(lay)
    if p_back is not None and p_lay is not None:
        mid = (p_back + p_lay) / 2.0
        half = max(default_half_spread, abs(p_back - p_lay) / 2.0)
        return _quote_cents(mid, half)
    mid = _decimal_to_prob(change.get("ltp"))
    if mid is None:
        return None
    return _quote_cents(mid, default_half_spread)


def parse_stream_path(path: str | Path) -> list[MarketTimeline]:
    """Parse all MATCH_ODDS markets under ``path`` (file or directory) into timelines."""
    # group messages per market_id across all sources.
    per_market: dict[str, list[tuple[int, dict[str, Any]]]] = {}
    is_match_odds: dict[str, bool] = {}
    for name, stream in _iter_text_streams(path):
        for line in stream:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            pt = msg.get("pt")
            for mc in msg.get("mc", []):
                mid = mc.get("id")
                if mid is None:
                    continue
                mdef = mc.get("marketDefinition")
                if mdef is not None:
                    is_match_odds[mid] = mdef.get("marketType") == "MATCH_ODDS"
                per_market.setdefault(mid, []).append((int(pt) if pt is not None else 0, mc))
    timelines: list[MarketTimeline] = []
    for mid, msgs in per_market.items():
        if not is_match_odds.get(mid, False):
            continue
        msgs.sort(key=lambda pm: pm[0])
        timelines.append(_build_timeline(mid, msgs))
    return timelines


# --------------------------------------------------------------------------- #
# Merge into StatsBomb match dicts
# --------------------------------------------------------------------------- #
def _norm_key(home: str, away: str) -> tuple[str, str]:
    return (canonical_team(home) or home.lower(), canonical_team(away) or away.lower())


def _dates_match(sb_date: str | None, mt: datetime | None) -> bool:
    """True if a StatsBomb match_date and a Betfair marketTime are within ±1 day."""
    if not sb_date or mt is None:
        return True  # don't reject on missing dates; team match still has to hold
    try:
        d0 = date.fromisoformat(str(sb_date)[:10])
    except ValueError:
        return True
    return abs((mt.date() - d0).days) <= 1


def merge_markets(
    matches: list[dict[str, Any]],
    timelines: list[MarketTimeline],
    *,
    tolerance: int = 2,
) -> tuple[list[dict[str, Any]], MergeReport]:
    """Inject Betfair quotes into the nearest tick of each StatsBomb match (by teams+date).

    The pre-off line is written onto the earliest tick so it becomes the CLV ``preoff``
    reference (see ``replay.py``). Mutates and returns the same match dicts.
    """
    report = MergeReport()
    used: set[int] = set()
    for match in matches:
        home, away = match["home_team"], match["away_team"]
        sb_date = match.get("metadata", {}).get("match_date")
        chosen: tuple[int, MarketTimeline, dict, dict] | None = None
        for i, tl in enumerate(timelines):
            if i in used:
                continue
            pre, inplay = tl.as_outcomes(home, away)
            if (pre or inplay) and _dates_match(sb_date, tl.market_time):
                chosen = (i, tl, pre or {}, inplay)
                break
        if chosen is None:
            report.unmatched_statsbomb.append(match["match_id"])
            continue
        i, tl, pre, inplay = chosen
        used.add(i)
        report.matched += 1
        report.clock_modes[tl.clock_mode] = report.clock_modes.get(tl.clock_mode, 0) + 1

        ticks = match["ticks"]
        # Pre-off line -> earliest tick (becomes the primary CLV reference).
        if pre:
            ticks[0].setdefault("markets", {}).update(pre)
        # In-play quotes -> most recent PAST-OR-PRESENT quote within tolerance (never
        # overwrite, never on FT). Carry-forward-only, matching how a live feed behaves:
        # nearest-by-absolute-distance could attach the quote from minute m+1/m+2 to a
        # tick at m — if a goal lands at m+1, the strategy "trades" at m against a price
        # that already contains the goal (lookahead in the edge-vs-Betfair measurement).
        for tick in ticks:
            if tick.get("period") == "FT" or "markets" in tick or not inplay:
                continue
            past = [mm for mm in inplay if mm <= tick["minute"]]
            if past and tick["minute"] - max(past) <= tolerance:
                tick["markets"] = inplay[max(past)]
        match.setdefault("metadata", {}).update({
            "betfair_market_id": tl.market_id,
            "betfair_clock_mode": tl.clock_mode,
            "price_source": "betfair",
        })
    report.unmatched_betfair = [
        tl.market_id for i, tl in enumerate(timelines) if i not in used
    ]
    return matches, report
