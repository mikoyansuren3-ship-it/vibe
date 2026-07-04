"""API-Football (api-sports.io) live provider — PRIMARY real feed.

The pure mapping functions (``snapshot_from_payload`` and friends) are deliberately
separated from the network code so they are unit-tested offline against a captured
sample payload — we never need a live key or network access to test the mapping.

Cost note: API-Football's free tier is 100 req/day. Fetching per-fixture statistics
multiplies requests, so live polling needs a paid plan (see research.md §2). The
default runtime provider is the simulator precisely so we never silently burn quota.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import TYPE_CHECKING, Any

import httpx

from ...logging_setup import get_logger
from ...models.schemas import MatchContext, MatchPeriod, MatchSnapshot, TeamStats
from ...modeling.ratings import apply_ratings, is_knockout_round
from ..http import request_with_retry
from .base import FootballDataProvider

if TYPE_CHECKING:
    from ..budget import RequestBudget

log = get_logger("football.apifootball")

_PERIOD_MAP = {
    "NS": MatchPeriod.PRE,
    "1H": MatchPeriod.FIRST_HALF,
    "HT": MatchPeriod.HALF_TIME,
    "2H": MatchPeriod.SECOND_HALF,
    "ET": MatchPeriod.ET_FIRST,
    "BT": MatchPeriod.ET_BREAK,
    "P": MatchPeriod.PENALTIES,
    "FT": MatchPeriod.FULL_TIME,
    "AET": MatchPeriod.FULL_TIME,
    "PEN": MatchPeriod.FULL_TIME,
}


# Abnormal fixture statuses with no valid 90' result: interrupted, suspended, abandoned,
# postponed, cancelled, walkover, technical-loss/awarded, to-be-defined. These must NOT be
# mislabelled as "1H live" or fed into calibration/CLV — they get status "abandoned".
_VOID_STATUSES = frozenset({"INT", "SUSP", "ABD", "PST", "CANC", "WO", "AWD", "TBD"})


def parse_period(short: str | None) -> MatchPeriod:
    return _PERIOD_MAP.get((short or "").upper(), MatchPeriod.FIRST_HALF)


def _to_float(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, str):
        value = value.strip().rstrip("%")
        if value in {"", "-"}:
            return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _to_int(value: Any) -> int:
    return int(round(_to_float(value)))


def team_stats_from_statistics(stat_block: dict[str, Any] | None) -> TeamStats:
    """Map one team's API-Football ``statistics`` array into ``TeamStats``."""
    stats = TeamStats()
    if not stat_block:
        return stats
    items = {
        str(item.get("type", "")).strip().lower(): item.get("value")
        for item in stat_block.get("statistics", [])
    }
    stats.shots = _to_int(items.get("total shots"))
    stats.shots_on_target = _to_int(items.get("shots on goal"))
    stats.corners = _to_int(items.get("corner kicks"))
    stats.offsides = _to_int(items.get("offsides"))
    stats.fouls = _to_int(items.get("fouls"))
    stats.yellow_cards = _to_int(items.get("yellow cards"))
    stats.red_cards = _to_int(items.get("red cards"))
    stats.gk_saves = _to_int(items.get("goalkeeper saves"))
    # API-Football's in-play WC statistics omit ``expected_goals`` entirely. Leave xg
    # as None when absent (do NOT coerce to 0.0) so the model can fall back to the
    # shot-based proxy / prior instead of reading "no xG" as "no chances created".
    # "Absent" includes the empty-string / "-" placeholders the API uses for blank
    # fields — _to_float would turn those into a fake "real xG = 0.0" that suppresses
    # the proxy, the exact failure the None contract exists to prevent.
    xg_raw = items.get("expected_goals")
    if isinstance(xg_raw, str):
        xg_raw = xg_raw.strip()
    stats.xg = _to_float(xg_raw) if xg_raw not in (None, "", "-") else None
    poss = _to_float(items.get("ball possession"))
    stats.possession = poss / 100.0 if poss > 1.0 else (poss or 0.5)
    pa = _to_float(items.get("passes %"))
    stats.pass_accuracy = pa / 100.0 if pa > 1.0 else pa
    stats.big_chances = _to_int(items.get("big chances"))
    return stats


def _index_statistics(stats_response: list[dict[str, Any]] | None) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for block in stats_response or []:
        name = (block.get("team") or {}).get("name")
        if name:
            out[name] = block
    return out


def snapshot_from_payload(
    fixture: dict[str, Any],
    stats_response: list[dict[str, Any]] | None = None,
    events_response: list[dict[str, Any]] | None = None,
) -> MatchSnapshot:
    """Build a normalized ``MatchSnapshot`` from API-Football JSON blocks."""
    fx = fixture.get("fixture", {})
    teams = fixture.get("teams", {})
    goals = fixture.get("goals", {})
    home_name = (teams.get("home") or {}).get("name", "Home")
    away_name = (teams.get("away") or {}).get("name", "Away")
    status = fx.get("status", {})
    short = status.get("short")
    period = parse_period(short)

    by_team = _index_statistics(stats_response)
    home_stats = team_stats_from_statistics(by_team.get(home_name))
    away_stats = team_stats_from_statistics(by_team.get(away_name))

    # Statistics can omit or LAG red cards (events land minutes earlier — exactly the
    # window the model's biggest in-play rate shock matters most). Count dismissals per
    # team from events — including second yellows, whose detail reads "Second Yellow
    # card" with no "red" in it — and take the max of the two sources, so a second
    # sending-off is never dropped because the first was already counted.
    if events_response:
        ev_home = ev_away = 0
        for ev in events_response:
            if str(ev.get("type", "")).lower() != "card":
                continue
            detail = str(ev.get("detail", "")).lower()
            if "red" not in detail and "second yellow" not in detail:
                continue
            ev_team = (ev.get("team") or {}).get("name")
            if ev_team == home_name:
                ev_home += 1
            elif ev_team == away_name:
                ev_away += 1
        home_stats.red_cards = max(home_stats.red_cards, ev_home)
        away_stats.red_cards = max(away_stats.red_cards, ev_away)

    venue = (fx.get("venue") or {}).get("name")
    # Inject real pre-match priors (Elo + neutral-venue) so the LIVE model isn't a flat
    # constant. Explicit ratings on an incoming context would win; here we start fresh.
    context = apply_ratings(MatchContext(venue=venue), home_name, away_name, venue=venue)
    # Competition round drives knockout markets (to-advance / extra time / penalties).
    round_label = (fixture.get("league") or {}).get("round")
    context.round = round_label
    context.is_knockout = is_knockout_round(round_label)

    if (short or "").upper() in _VOID_STATUSES:
        status_str = "abandoned"  # interrupted/suspended/postponed/etc — no valid 90' result
    else:
        status_str = (
            "finished" if period.is_finished else ("live" if period.is_live else "scheduled")
        )
    # Settle on the 90' REGULATION score (Kalshi WC contracts exclude ET/penalties). For a
    # finished match prefer score.fulltime; in-play we use the running goals.
    home_score, away_score = _to_int(goals.get("home")), _to_int(goals.get("away"))
    if period.is_finished:
        ft = fixture.get("score", {}).get("fulltime") or {}
        if ft.get("home") is not None and ft.get("away") is not None:
            home_score, away_score = _to_int(ft.get("home")), _to_int(ft.get("away"))
    return MatchSnapshot(
        match_id=str(fx.get("id", f"{home_name}-{away_name}")),
        provider="apifootball",
        home_team=home_name,
        away_team=away_name,
        minute=_to_int(status.get("elapsed")),
        period=period,
        home_score=home_score,
        away_score=away_score,
        home=home_stats,
        away=away_stats,
        status=status_str,
        context=context,
        # Persist the events feed alongside the fixture so goal/card/sub timing + players
        # are replayable later (goal-timing, per-half settlement, player-prop seeds).
        raw={**fixture, "events": events_response} if events_response is not None else fixture,
    )


def _parse_kickoff(value: Any) -> "datetime | None":
    """Parse an API-Football ISO-8601 fixture date (e.g. '2026-06-30T18:00:00+00:00')."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def upcoming_from_payload(fixture: dict[str, Any]) -> MatchSnapshot:
    """Build a PRE-period ``MatchSnapshot`` for an upcoming fixture (no stats yet).

    Reuses ``snapshot_from_payload`` — which already injects Elo priors via
    ``apply_ratings`` — then forces the PRE/scheduled state (a ``/fixtures?next`` entry
    is by definition not started) and attaches the scheduled kickoff so projections can
    be ordered and labelled. Pure: unit-tested offline against a captured payload."""
    snap = snapshot_from_payload(fixture)
    snap.period = MatchPeriod.PRE
    snap.status = "scheduled"
    snap.minute = 0
    snap.home_score = 0
    snap.away_score = 0
    kickoff = _parse_kickoff((fixture.get("fixture") or {}).get("date"))
    if snap.context is not None and kickoff is not None:
        snap.context.kickoff = kickoff
    return snap


def parse_lineups(resp: list[dict[str, Any]] | None, home: str, away: str) -> dict[str, Any]:
    """Map API-Football /fixtures/lineups into {home/away: {formation, xi:[names]}}."""
    out = {"home": {"formation": None, "xi": []}, "away": {"formation": None, "xi": []}}
    for block in resp or []:
        name = (block.get("team") or {}).get("name")
        key = "home" if name == home else "away" if name == away else None
        if not key:
            continue
        out[key]["formation"] = block.get("formation")
        out[key]["xi"] = [
            (e.get("player") or {}).get("name")
            for e in (block.get("startXI") or [])
            if (e.get("player") or {}).get("name")
        ]
    return out


def parse_injuries(resp: list[dict[str, Any]] | None, home: str, away: str) -> dict[str, list[str]]:
    """Map API-Football /injuries into {home/away: [player names]}."""
    out: dict[str, list[str]] = {"home": [], "away": []}
    for item in resp or []:
        name = (item.get("team") or {}).get("name")
        player = (item.get("player") or {}).get("name")
        if not player:
            continue
        if name == home:
            out["home"].append(player)
        elif name == away:
            out["away"].append(player)
    return out


def apply_context(snap: MatchSnapshot, lineups: dict | None, injuries: dict | None) -> None:
    """Attach parsed lineups + injuries onto a snapshot's MatchContext (in place)."""
    c = snap.context
    if c is None:
        return
    lu, inj = lineups or {}, injuries or {}
    c.home_formation = (lu.get("home") or {}).get("formation")
    c.away_formation = (lu.get("away") or {}).get("formation")
    c.home_xi = (lu.get("home") or {}).get("xi", [])
    c.away_xi = (lu.get("away") or {}).get("xi", [])
    c.home_injuries = inj.get("home", [])
    c.away_injuries = inj.get("away", [])


class APIFootballProvider(FootballDataProvider):
    name = "apifootball"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://v3.football.api-sports.io",
        timeout: float = 10.0,
        max_retries: int = 3,
        fetch_statistics: bool = True,
        fetch_context: bool = True,
        fetch_events: bool = True,
        league_id: int | None = None,
        budget: "RequestBudget | None" = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.max_retries = max_retries
        self.fetch_statistics = fetch_statistics
        self.fetch_context = fetch_context  # lineups + injuries (fetched once per match)
        self.fetch_events = fetch_events  # goals/cards/subs w/ minute+player
        self._ctx_cache: dict[int, dict[str, Any]] = {}
        # When set, only poll this league's live fixtures (e.g. 1 = FIFA World Cup).
        self.league_id = league_id
        self._budget = budget
        self._client = httpx.AsyncClient(
            timeout=timeout, headers={"x-apisports-key": api_key}
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get(self, endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
        if self._budget is not None:
            await self._budget.acquire()
        resp = await request_with_retry(
            self._client,
            "GET",
            f"{self.base_url}{endpoint}",
            params=params,
            max_retries=self.max_retries,
        )
        resp.raise_for_status()
        return resp.json()

    async def fetch_live(self) -> list[MatchSnapshot]:
        # API-Football's `live=<league_id>` filter is unreliable (returns nothing on the
        # free tier even when that league has a live match), so we always pull `live=all`
        # — still a single request — and filter by league id CLIENT-SIDE.
        data = await self._get("/fixtures", {"live": "all"})
        fixtures = data.get("response", [])
        if self.league_id is not None:
            fixtures = [
                f for f in fixtures if (f.get("league") or {}).get("id") == self.league_id
            ]
        # Fan the per-fixture statistics/events/context fetches out concurrently rather
        # than 1+2N serial round-trips, so the last match's quote is no longer N RTTs
        # staler than the first. The shared RequestBudget still bounds the aggregate rate;
        # gather only overlaps the network waits, it doesn't lift the token ceiling.
        snaps = await asyncio.gather(*(self._assemble_live(f) for f in fixtures))
        return [s for s in snaps if s is not None]

    async def _assemble_live(self, fixture: dict[str, Any]) -> MatchSnapshot | None:
        """Build one live snapshot, gathering its statistics + events concurrently.

        A single malformed fixture is logged and dropped rather than sinking the whole
        poll — the sub-fetches already degrade to ``None`` on their own errors, so this
        guard only catches a mapping failure on one fixture's payload.
        """
        fixture_id = (fixture.get("fixture") or {}).get("id")
        try:
            stats, events = await asyncio.gather(
                self._fetch_statistics(fixture_id),
                self._fetch_events(fixture_id),
            )
            snap = snapshot_from_payload(fixture, stats, events)
            if self.fetch_context and fixture_id is not None:
                await self._apply_context(snap, fixture_id)
            return snap
        except Exception as exc:  # one bad fixture must not drop every live match
            log.warning("live fixture assembly failed", extra={"err": str(exc)})
            return None

    async def fetch_upcoming(self, limit: int = 8) -> list[MatchSnapshot]:
        """Upcoming (not-yet-started) fixtures for pre-match projection. A single
        ``/fixtures?next=N`` request, league filtered client-side (like ``fetch_live``).
        No statistics/lineups fetched — none exist pre-match, so no extra quota burn."""
        params: dict[str, Any] = {"next": str(max(1, limit))}
        if self.league_id is not None:
            params["league"] = str(self.league_id)
        data = await self._get("/fixtures", params)
        fixtures = data.get("response", [])
        if self.league_id is not None:
            fixtures = [
                f for f in fixtures if (f.get("league") or {}).get("id") == self.league_id
            ]
        return [upcoming_from_payload(f) for f in fixtures[: max(0, limit)]]

    async def fetch_fixture(self, match_id: str) -> MatchSnapshot | None:
        """Fetch one fixture by id in any state (used to capture the final/settled score)."""
        try:
            data = await self._get("/fixtures", {"id": str(match_id)})
        except Exception as exc:
            log.warning("fixture fetch failed", extra={"id": match_id, "err": str(exc)})
            return None
        resp = data.get("response", [])
        if not resp:
            return None
        fixture = resp[0]
        fixture_id = (fixture.get("fixture") or {}).get("id")
        stats, events = await asyncio.gather(
            self._fetch_statistics(fixture_id),
            self._fetch_events(fixture_id),
        )
        return snapshot_from_payload(fixture, stats, events)

    async def _fetch_statistics(self, fixture_id: Any) -> list[dict[str, Any]] | None:
        """Per-team in-play statistics (shots, xG when present, cards, possession…)."""
        if not self.fetch_statistics or fixture_id is None:
            return None
        try:
            return (
                await self._get("/fixtures/statistics", {"fixture": fixture_id})
            ).get("response")
        except Exception as exc:  # degrade gracefully
            log.warning("statistics fetch failed", extra={"err": str(exc)})
            return None

    async def _fetch_events(self, fixture_id: Any) -> list[dict[str, Any]] | None:
        """Goals/cards/subs with minute + player (for goal timing, per-half, props)."""
        if not self.fetch_events or fixture_id is None:
            return None
        try:
            return (await self._get("/fixtures/events", {"fixture": fixture_id})).get("response")
        except Exception as exc:  # degrade gracefully
            log.warning("events fetch failed", extra={"err": str(exc)})
            return None

    async def _apply_context(self, snap: MatchSnapshot, fixture_id: int) -> None:
        """Fetch lineups + injuries once per fixture (cached) and attach to the snapshot."""
        if fixture_id not in self._ctx_cache:
            lineups = injuries = None
            try:
                lineups = parse_lineups(
                    (await self._get("/fixtures/lineups", {"fixture": fixture_id})).get("response"),
                    snap.home_team, snap.away_team,
                )
            except Exception as exc:
                log.warning("lineups fetch failed", extra={"err": str(exc)})
            try:
                injuries = parse_injuries(
                    (await self._get("/injuries", {"fixture": fixture_id})).get("response"),
                    snap.home_team, snap.away_team,
                )
            except Exception as exc:
                log.warning("injuries fetch failed", extra={"err": str(exc)})
            self._ctx_cache[fixture_id] = {"lineups": lineups, "injuries": injuries}
        cached = self._ctx_cache[fixture_id]
        apply_context(snap, cached.get("lineups"), cached.get("injuries"))
