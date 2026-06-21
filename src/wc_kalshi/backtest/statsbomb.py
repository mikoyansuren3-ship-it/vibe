"""StatsBomb open-data → ``historical.py`` loader.

Converts **free per-shot xG** (and goals / red cards) from past World Cups into the
exact match-dict format ``backtest/historical.py`` consumes, so ``wck historical`` can
score the model against **real** in-play timelines.

Honesty caveat (the whole point of this module): StatsBomb has **no market prices**, so
on its own this produces a **real calibration** number (Brier / log-loss / ECE on real
World-Cup outcomes) — **not** CLV or tradable edge. Attach real exchange prices with
``backtest/betfair.py`` to measure edge against a real line.

What we model is the **90′ regulation 1X2** (the resolvable event for a WC match market;
Kalshi WC contracts settle "after 90 minutes plus stoppage time … not extra time or
penalties"). So we keep only StatsBomb ``period`` 1 and 2 and settle on the score at 90′.
A knockout level at 90′ therefore settles as a **DRAW** here — correct for a 90′ market
even though ET/penalties later broke the tie.

StatsBomb time semantics (verified against real data, e.g. 2022 final 3869685):
  * ``minute`` is period-based-continuous, NOT reset per period: the 1st half runs
    0→45+ (stoppage can reach the low 50s) and the 2nd half *starts at minute 45* and
    runs 45→90+. So 1st-half stoppage (min 46–52) overlaps 2nd-half regulation (min 45+).
    => order events by ``(period, minute, second, index)`` and clamp each side to its
    own half when laying them on a 0..90 grid.

Open data: https://github.com/statsbomb/open-data (CC BY-NC-SA — attribution required).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

from ..logging_setup import get_logger
from ..modeling.ratings import canonical_team, elo_for

log = get_logger("statsbomb")

STATSBOMB_RAW = "https://raw.githubusercontent.com/statsbomb/open-data/master/data"

# Men's & women's FIFA World Cups in open data that carry per-shot xG.
# (competition_id, season_id) -> human label. Verified against competitions.json.
WORLD_CUPS: dict[tuple[int, int], str] = {
    (43, 106): "FIFA World Cup 2022",
    (43, 3): "FIFA World Cup 2018",
    (72, 107): "Women's World Cup 2023",
    (72, 30): "Women's World Cup 2019",
}

# StatsBomb card names that remove a player for the rest of the match.
_RED_CARD_NAMES = frozenset({"Red Card", "Second Yellow"})
_REGULATION_PERIODS = frozenset({1, 2})


def _match_minute(period: int, minute: int, settle_minute: int) -> int:
    """Lay a StatsBomb (period, minute) onto a clean 0..settle_minute match axis.

    ``minute`` is already period-based-continuous (2nd half starts at 45), so we only
    clamp each half into its own band and never add an offset.
    """
    if period == 1:
        return max(0, min(minute, 45))
    # period == 2 (the only other regulation period we keep): already 45-based.
    return max(45, min(minute, settle_minute))


def _team_side(name: str | None, home: str, away: str) -> str | None:
    """Resolve an event team name to "home"/"away" via canonicalised names."""
    c = canonical_team(name)
    if c is not None:
        if c == canonical_team(home):
            return "home"
        if c == canonical_team(away):
            return "away"
    # Fall back to raw string match (handles names absent from the rating table).
    if name == home:
        return "home"
    if name == away:
        return "away"
    return None


def _elo(name: str, elo_table: dict[str, float] | None) -> float | None:
    """Date-appropriate Elo if an explicit table is supplied, else the built-in prior."""
    if elo_table:
        c = canonical_team(name) or (name or "").strip()
        for key in (c, name):
            if key in elo_table:
                return float(elo_table[key])
    return elo_for(name)


def convert_match(
    match_meta: dict[str, Any],
    events: list[dict[str, Any]],
    *,
    settle_minute: int = 90,
    elo_table: dict[str, float] | None = None,
) -> dict[str, Any] | None:
    """Convert one StatsBomb match (metadata + event stream) to a historical dict.

    Returns ``None`` (with a warning) for matches that carry no ``statsbomb_xg`` at all
    (older seasons), since an xG-blind timeline would not exercise the model's edge.
    """
    home = str(match_meta["home_team"]["home_team_name"])
    away = str(match_meta["away_team"]["away_team_name"])
    sb_id = match_meta.get("match_id")

    # Order strictly by (period, minute, second, index): a pure minute-sort would
    # interleave 1st-half stoppage with the 2nd half (see module docstring).
    reg = sorted(
        (e for e in events if e.get("period") in _REGULATION_PERIODS),
        key=lambda e: (e["period"], e.get("minute", 0), e.get("second", 0), e.get("index", 0)),
    )

    have_xg = any(
        e["type"]["name"] == "Shot" and e.get("shot", {}).get("statsbomb_xg") is not None
        for e in reg
    )
    if not have_xg:
        log.warning("match %s (%s v %s) has no statsbomb_xg — skipping", sb_id, home, away)
        return None

    # Walk events once, recording the cumulative state at each event's match-minute.
    cum = {"home_xg": 0.0, "away_xg": 0.0, "home_score": 0, "away_score": 0,
           "home_red": 0, "away_red": 0}
    timeline: list[tuple[int, dict[str, Any]]] = []  # (match_minute, snapshot of cum)

    for e in reg:
        m = _match_minute(e["period"], e.get("minute", 0), settle_minute)
        etype = e["type"]["name"]
        side = _team_side(e.get("team", {}).get("name"), home, away)

        if etype == "Shot":
            shot = e.get("shot", {})
            xg = shot.get("statsbomb_xg")
            if xg is not None and side:
                cum[f"{side}_xg"] += float(xg)
            if shot.get("outcome", {}).get("name") == "Goal" and side:
                cum[f"{side}_score"] += 1
        elif etype == "Own Goal For" and side:
            # "Own Goal For" credits the beneficiary team a goal.
            cum[f"{side}_score"] += 1
        elif etype in ("Bad Behaviour", "Foul Committed"):
            card = (e.get(_EVENT_KEY[etype], {}) or {}).get("card", {}) or {}
            if card.get("name") in _RED_CARD_NAMES and side:
                cum[f"{side}_red"] += 1

        timeline.append((m, dict(cum)))

    # Densify onto integer minutes 0..settle_minute, carrying the latest state ≤ minute.
    ticks: list[dict[str, Any]] = []
    idx = 0
    state = {"home_xg": 0.0, "away_xg": 0.0, "home_score": 0, "away_score": 0,
             "home_red": 0, "away_red": 0}
    for minute in range(0, settle_minute + 1):
        while idx < len(timeline) and timeline[idx][0] <= minute:
            state = timeline[idx][1]
            idx += 1
        period = "FT" if minute == settle_minute else ("1H" if minute < 45 else "2H")
        ticks.append({
            "minute": minute, "period": period,
            "home_score": state["home_score"], "away_score": state["away_score"],
            "home_xg": round(state["home_xg"], 4), "away_xg": round(state["away_xg"], 4),
            "home_red": state["home_red"], "away_red": state["away_red"],
        })

    home_elo, away_elo = _elo(home, elo_table), _elo(away, elo_table)
    return {
        "match_id": f"SB-{sb_id}",
        "home_team": home,
        "away_team": away,
        "home_elo": home_elo,
        "away_elo": away_elo,
        "neutral_venue": True,  # all WC venues are neutral for settlement (Kalshi terms)
        "ticks": ticks,
        "metadata": {
            "source": "statsbomb",
            "statsbomb_match_id": sb_id,
            "competition": match_meta.get("competition", {}).get("competition_name"),
            "season": match_meta.get("season", {}).get("season_name"),
            "match_date": match_meta.get("match_date"),
            "stage": match_meta.get("competition_stage", {}).get("name"),
            "elo_source": "elo_table" if elo_table else "builtin_ratings_2026",
            "elo_coverage": {"home": home_elo is not None, "away": away_elo is not None},
        },
    }


# StatsBomb stores the card under a per-event-type key.
_EVENT_KEY = {"Bad Behaviour": "bad_behaviour", "Foul Committed": "foul_committed"}


# --------------------------------------------------------------------------- #
# Fetch (network optional: local clone via repo=, else cached raw GitHub)
# --------------------------------------------------------------------------- #
def _load_json(
    rel: str, *, repo: str | Path | None, cache_dir: Path, client: httpx.Client | None
) -> Any:
    """Load ``data/<rel>`` from a local clone, the on-disk cache, or raw GitHub."""
    if repo is not None:
        return json.loads((Path(repo) / "data" / rel).read_text())
    cached = cache_dir / rel
    if cached.exists():
        return json.loads(cached.read_text())
    assert client is not None
    resp = client.get(f"{STATSBOMB_RAW}/{rel}", timeout=60.0)
    resp.raise_for_status()
    cached.parent.mkdir(parents=True, exist_ok=True)
    cached.write_text(resp.text)
    return resp.json()


def build_world_cup(
    competition: int,
    season: int,
    *,
    repo: str | Path | None = None,
    cache_dir: str | Path = "data/statsbomb",
    elo_table: dict[str, float] | None = None,
    settle_minute: int = 90,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Fetch + convert every match of one World Cup into historical dicts."""
    cache = Path(cache_dir)
    client = httpx.Client() if repo is None else None
    out: list[dict[str, Any]] = []
    try:
        matches = _load_json(
            f"matches/{competition}/{season}.json", repo=repo, cache_dir=cache, client=client
        )
        if limit:
            matches = matches[:limit]
        for meta in matches:
            mid = meta["match_id"]
            try:
                events = _load_json(
                    f"events/{mid}.json", repo=repo, cache_dir=cache, client=client
                )
            except Exception as exc:  # one missing match must not abort the season
                log.warning("could not load events for %s: %s", mid, exc)
                continue
            conv = convert_match(
                meta, events, settle_minute=settle_minute, elo_table=elo_table
            )
            if conv is not None:
                out.append(conv)
    finally:
        if client is not None:
            client.close()
    return out


def coverage_report(matches: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarise Elo / tick coverage for the converted matches (printed by the CLI)."""
    n = len(matches)
    full_elo = sum(
        1 for m in matches
        if m["metadata"]["elo_coverage"]["home"] and m["metadata"]["elo_coverage"]["away"]
    )
    missing = [
        m["match_id"] for m in matches
        if not (m["metadata"]["elo_coverage"]["home"] and m["metadata"]["elo_coverage"]["away"])
    ]
    return {
        "n_matches": n,
        "full_elo": full_elo,
        "partial_or_missing_elo": n - full_elo,
        "missing_elo_match_ids": missing[:20],
        "avg_ticks": round(sum(len(m["ticks"]) for m in matches) / n, 1) if n else 0,
    }
