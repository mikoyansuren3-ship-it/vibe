"""National-team rating priors for the LIVE model.

The biggest gap between the tested model and the live one was data: the simulator
injects per-team Elo, but the live providers hard-coded ``neutral_venue=True`` and
left ``home_elo``/``away_elo`` unset, so every live match collapsed to the same flat
constant prior. This module is the single source of pre-match strength priors that
both live providers consult, so the live model is no weaker than the one we backtest.

The Elo numbers below are illustrative World-Football-Elo-scale values and are meant
to be **replaced by a maintained feed** (e.g. eloratings.net) in production; the point
is the wiring, not these exact numbers. ``apply_ratings`` only fills values that are
missing, so an explicit feed or test fixture always wins.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from ..models.schemas import MatchContext

# Round labels that denote a knockout tie (no draw — extra time / penalties decide it).
_KNOCKOUT_RE = re.compile(r"round of|final|quarter|semi|knockout|play-?off|1/\d|last \d", re.I)


def is_knockout_round(round_label: str | None) -> bool:
    """True if a competition round string (e.g. API-Football ``league.round``) denotes a
    knockout tie. False for the group stage or an unknown/empty label."""
    if not round_label:
        return False
    if "group" in round_label.lower():
        return False
    return bool(_KNOCKOUT_RE.search(round_label))


# Approximate World-Football-Elo strength for likely 2026 World Cup nations.
TEAM_ELO: dict[str, float] = {
    "Argentina": 2140, "France": 2080, "Netherlands": 2030, "Brazil": 2020,
    "England": 2010, "Portugal": 2000, "Spain": 2050, "Italy": 1960, "Germany": 1960,
    "Belgium": 1950, "Croatia": 1900, "Uruguay": 1900, "Colombia": 1890,
    "Switzerland": 1890, "Morocco": 1870, "Denmark": 1860, "Japan": 1830,
    "Norway": 1830, "Senegal": 1820, "Ecuador": 1820, "Serbia": 1820, "Austria": 1820,
    "Turkey": 1820, "Greece": 1810, "Sweden": 1810, "Ukraine": 1810, "Iran": 1810,
    "Nigeria": 1810, "Algeria": 1800, "Mexico": 1800, "USA": 1790, "Egypt": 1790,
    "South Korea": 1790, "Chile": 1790, "Ivory Coast": 1790, "Czechia": 1790,
    "Romania": 1790, "Scotland": 1790, "Poland": 1780, "Slovakia": 1780,
    "Hungary": 1780, "Peru": 1770, "Slovenia": 1770, "Georgia": 1770, "Wales": 1760,
    "Canada": 1760, "Mali": 1760, "Albania": 1760, "Cameroon": 1760, "Venezuela": 1760,
    "Paraguay": 1740, "Australia": 1740, "DR Congo": 1740, "Finland": 1740,
    "Ireland": 1740, "Ghana": 1730, "Tunisia": 1730, "South Africa": 1730,
    "Burkina Faso": 1730, "Iceland": 1730, "Saudi Arabia": 1700, "Iraq": 1700,
    "Costa Rica": 1700, "Cape Verde": 1700, "Bolivia": 1690, "Qatar": 1680,
    "Panama": 1680, "Jamaica": 1660, "UAE": 1660, "Honduras": 1660,
    "New Zealand": 1620,
    # Added for the 2026 field (illustrative, like the rest — replace with a real feed).
    "Bosnia and Herzegovina": 1740, "Uzbekistan": 1630, "Jordan": 1560,
    "Haiti": 1520, "Curacao": 1500,
}

# 2026 World Cup hosts — used for the neutral-venue heuristic.
HOST_NATIONS: frozenset[str] = frozenset({"USA", "Canada", "Mexico"})

# Common provider spellings -> our canonical key.
_ALIASES: dict[str, str] = {
    "united states": "USA",
    "united states of america": "USA",
    "us": "USA",
    "korea republic": "South Korea",
    "republic of korea": "South Korea",
    "south korea": "South Korea",
    "ir iran": "Iran",
    "iran": "Iran",
    "côte d'ivoire": "Ivory Coast",
    "cote d'ivoire": "Ivory Coast",
    "czech republic": "Czechia",
    "türkiye": "Turkey",
    "turkiye": "Turkey",
    "cabo verde": "Cape Verde",
    "congo dr": "DR Congo",
    "dr congo": "DR Congo",
    "democratic republic of the congo": "DR Congo",
    "republic of ireland": "Ireland",
    "united arab emirates": "UAE",
    # Provider naming variants (e.g. API-Football) -> our canonical key.
    "cape verde islands": "Cape Verde",
    "bosnia & herzegovina": "Bosnia and Herzegovina",
    "curaçao": "Curacao",
    "ivory coast": "Ivory Coast",
}

_CANON = {k.lower(): k for k in TEAM_ELO}


def canonical_team(name: str | None) -> str | None:
    """Resolve a provider team name to a canonical key, or None if unknown."""
    if not name:
        return None
    key = name.strip().lower()
    if key in _ALIASES:
        return _ALIASES[key]
    return _CANON.get(key)


# Maintained-feed overlay. A JSON {team: elo} file pointed at by WCK_ELO_TABLE (or set via
# set_active_elo_table) REPLACES the illustrative built-in ratings with no code edit — the
# "replace with a real feed" hook this module promises. ``None`` = not yet initialised.
_OVERRIDE: dict[str, float] | None = None


def load_elo_table(path: str | Path) -> dict[str, float]:
    """Load + canonicalize a maintained ``{team: elo}`` JSON feed."""
    raw = json.loads(Path(path).expanduser().read_text())
    return {(canonical_team(k) or k): float(v) for k, v in raw.items()}


def set_active_elo_table(table: dict[str, float] | None) -> None:
    """Install a maintained Elo feed as the live override (``None`` clears it)."""
    global _OVERRIDE
    _OVERRIDE = dict(table) if table else {}


def _active_override() -> dict[str, float]:
    global _OVERRIDE
    if _OVERRIDE is None:  # lazy first-use load from env (after dotenv has run)
        path = os.getenv("WCK_ELO_TABLE")
        try:
            _OVERRIDE = load_elo_table(path) if path else {}
        except (OSError, ValueError):
            _OVERRIDE = {}
    return _OVERRIDE


def elo_for(name: str | None) -> float | None:
    """Best-effort Elo for a team name (None if we have no rating). A maintained feed
    (WCK_ELO_TABLE / set_active_elo_table) wins over the illustrative built-in table."""
    canon = canonical_team(name)
    override = _active_override()
    if canon and canon in override:
        return override[canon]
    if name and name in override:
        return override[name]
    return TEAM_ELO.get(canon) if canon else None


def infer_neutral_venue(home_team: str | None, venue: str | None = None) -> bool:
    """Heuristic neutral-venue flag for WC 2026.

    Most World Cup games are neutral for both sides. The exception is a host nation
    playing in its own country, which gets a genuine home advantage. We lack a reliable
    stadium->country map from the providers, so we treat a host nation listed as the
    *home* side as non-neutral. Pass explicit data to override this.
    """
    return canonical_team(home_team) not in HOST_NATIONS


def apply_ratings(
    context: MatchContext | None, home_team: str, away_team: str, *, venue: str | None = None
) -> MatchContext:
    """Populate missing Elo + neutral_venue on a context (explicit values win)."""
    ctx = context or MatchContext()
    if ctx.home_elo is None:
        ctx.home_elo = elo_for(home_team)
    if ctx.away_elo is None:
        ctx.away_elo = elo_for(away_team)
    # Only override the default-True neutral flag when the home side is a host nation.
    if ctx.neutral_venue and not infer_neutral_venue(home_team, venue or ctx.venue):
        ctx.neutral_venue = False
    return ctx
