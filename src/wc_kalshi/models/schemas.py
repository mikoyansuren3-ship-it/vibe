"""Normalized, provider-agnostic schemas — the lingua franca of the pipeline.

Two ingestion families (football data, Kalshi markets) both normalize down to
``MatchSnapshot`` and ``MarketSnapshot``. Every snapshot is UTC-timestamped and
append-only: we never overwrite, so a whole match can be replayed tick-by-tick.
"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator

from ..util import utcnow


class Outcome(str, enum.Enum):
    HOME = "home"
    DRAW = "draw"
    AWAY = "away"


class Side(str, enum.Enum):
    YES = "yes"
    NO = "no"


class OrderAction(str, enum.Enum):
    BUY = "buy"
    SELL = "sell"


class MatchPeriod(str, enum.Enum):
    PRE = "pre"
    FIRST_HALF = "1H"
    HALF_TIME = "HT"
    SECOND_HALF = "2H"
    ET_FIRST = "ET1"
    ET_BREAK = "ETB"
    ET_SECOND = "ET2"
    PENALTIES = "PEN"
    FULL_TIME = "FT"

    @property
    def is_live(self) -> bool:
        return self in {
            MatchPeriod.FIRST_HALF,
            MatchPeriod.HALF_TIME,
            MatchPeriod.SECOND_HALF,
            MatchPeriod.ET_FIRST,
            MatchPeriod.ET_BREAK,
            MatchPeriod.ET_SECOND,
            MatchPeriod.PENALTIES,
        }

    @property
    def is_finished(self) -> bool:
        return self is MatchPeriod.FULL_TIME


# --------------------------------------------------------------------------- #
# Live football state
# --------------------------------------------------------------------------- #
class TeamStats(BaseModel):
    """Per-team live statistics. All counters are cumulative match totals."""

    # None = the provider did not supply expected goals for this tick (use the
    # shot-based proxy in modeling/xg_proxy.py); a float (including 0.0) is a real value.
    xg: float | None = None
    shots: int = 0
    shots_on_target: int = 0
    big_chances: int = 0
    possession: float = 0.5  # fraction 0..1
    pass_accuracy: float = 0.0  # fraction 0..1
    dangerous_attacks: int = 0
    corners: int = 0
    fouls: int = 0
    offsides: int = 0
    yellow_cards: int = 0
    red_cards: int = 0
    gk_saves: int = 0
    subs_used: int = 0


class MatchContext(BaseModel):
    """Pre-match / contextual priors (mostly static through a match)."""

    kickoff: datetime | None = None
    venue: str | None = None
    neutral_venue: bool = True  # World Cup: most games neutral for both teams
    round: str | None = None  # competition round label, e.g. "Group A", "Round of 16", "Final"
    is_knockout: bool = False  # knockout tie (no draw — goes to extra time / penalties)
    home_elo: float | None = None
    away_elo: float | None = None
    home_fifa_rank: int | None = None
    away_fifa_rank: int | None = None
    home_rest_days: float | None = None
    away_rest_days: float | None = None
    home_market_value_m: float | None = None  # squad value, EUR millions
    away_market_value_m: float | None = None
    temp_c: float | None = None
    humidity_pct: float | None = None
    # Pre-match context (populated by providers that expose it, e.g. API-Football).
    home_formation: str | None = None
    away_formation: str | None = None
    home_xi: list[str] = Field(default_factory=list)
    away_xi: list[str] = Field(default_factory=list)
    home_injuries: list[str] = Field(default_factory=list)
    away_injuries: list[str] = Field(default_factory=list)


class MatchSnapshot(BaseModel):
    """A single point-in-time observation of a live match."""

    match_id: str
    provider: str
    ts: datetime = Field(default_factory=utcnow)
    home_team: str
    away_team: str
    minute: int = 0  # match minute (0..120+)
    period: MatchPeriod = MatchPeriod.PRE
    added_time: int = 0  # stoppage minutes shown
    home_score: int = 0
    away_score: int = 0
    home: TeamStats = Field(default_factory=TeamStats)
    away: TeamStats = Field(default_factory=TeamStats)
    status: str = "scheduled"  # scheduled | live | finished
    context: MatchContext | None = None
    raw: dict[str, Any] | None = Field(default=None, repr=False)

    @field_validator("minute")
    @classmethod
    def _minute_nonneg(cls, v: int) -> int:
        return max(0, v)

    # -- derived helpers ------------------------------------------------- #
    @property
    def score_diff(self) -> int:
        """Home minus away goals."""
        return self.home_score - self.away_score

    @property
    def net_red_cards(self) -> int:
        """Positive => home has the man advantage (away sent off more)."""
        return self.away.red_cards - self.home.red_cards

    @property
    def total_minutes(self) -> int:
        """Nominal full-time minute for the current phase (90, or 120 in ET)."""
        if self.period in {MatchPeriod.ET_FIRST, MatchPeriod.ET_BREAK, MatchPeriod.ET_SECOND}:
            return 120
        return 90

    @property
    def minutes_remaining(self) -> float:
        return max(0.0, float(self.total_minutes) - float(self.minute))


# --------------------------------------------------------------------------- #
# Kalshi market state
# --------------------------------------------------------------------------- #
class BookLevel(BaseModel):
    price_cents: int  # 1..99
    size: int  # contracts


class MarketSnapshot(BaseModel):
    """A single point-in-time observation of one Kalshi Yes/No market.

    Kalshi quotes prices in integer cents (1..99). A Yes contract settles at $1.00
    if the event resolves Yes. ``outcome`` says which match outcome a Yes resolves.
    """

    market_ticker: str
    event_ticker: str | None = None
    match_id: str
    outcome: Outcome
    ts: datetime = Field(default_factory=utcnow)
    yes_bid: int | None = None  # cents
    yes_ask: int | None = None  # cents
    last_price: int | None = None  # cents
    volume: int = 0
    open_interest: int | None = None
    yes_depth: list[BookLevel] = Field(default_factory=list)
    no_depth: list[BookLevel] = Field(default_factory=list)
    status: str = "active"  # active | closed | settled
    settlement_rule: str | None = None

    # -- price helpers (all in probability units 0..1) ------------------- #
    @property
    def no_bid(self) -> int | None:
        """Best No bid = 100 - best Yes ask (Kalshi books are symmetric)."""
        return None if self.yes_ask is None else 100 - self.yes_ask

    @property
    def no_ask(self) -> int | None:
        return None if self.yes_bid is None else 100 - self.yes_bid

    @property
    def yes_mid_cents(self) -> float | None:
        if self.yes_bid is None or self.yes_ask is None:
            return float(self.last_price) if self.last_price is not None else None
        return (self.yes_bid + self.yes_ask) / 2.0

    @property
    def yes_mid_prob(self) -> float | None:
        mid = self.yes_mid_cents
        return None if mid is None else mid / 100.0

    @property
    def spread_cents(self) -> int | None:
        if self.yes_bid is None or self.yes_ask is None:
            return None
        return self.yes_ask - self.yes_bid


# --------------------------------------------------------------------------- #
# Model / market / edge outputs
# --------------------------------------------------------------------------- #
class Probabilities(BaseModel):
    """A normalized 1X2 probability vector that sums to 1."""

    match_id: str
    ts: datetime = Field(default_factory=utcnow)
    p_home: float
    p_draw: float
    p_away: float
    source: str = "model"
    meta: dict[str, Any] = Field(default_factory=dict)

    @field_validator("p_home", "p_draw", "p_away")
    @classmethod
    def _prob_range(cls, v: float) -> float:
        return min(1.0, max(0.0, v))

    def normalized(self) -> "Probabilities":
        total = self.p_home + self.p_draw + self.p_away
        if total <= 0:
            return self.model_copy(update={"p_home": 1 / 3, "p_draw": 1 / 3, "p_away": 1 / 3})
        return self.model_copy(
            update={
                "p_home": self.p_home / total,
                "p_draw": self.p_draw / total,
                "p_away": self.p_away / total,
            }
        )

    def get(self, outcome: Outcome) -> float:
        return {
            Outcome.HOME: self.p_home,
            Outcome.DRAW: self.p_draw,
            Outcome.AWAY: self.p_away,
        }[outcome]

    def as_dict(self) -> dict[Outcome, float]:
        return {Outcome.HOME: self.p_home, Outcome.DRAW: self.p_draw, Outcome.AWAY: self.p_away}


class ProposalStatus(str, enum.Enum):
    PENDING = "pending"
    EXECUTED = "executed"
    REJECTED = "rejected"
    EXPIRED = "expired"
    SUPERSEDED = "superseded"
    FAILED = "failed"


class TradeProposal(BaseModel):
    """An actionable trade awaiting a human decision (advisory mode).

    Carries everything a person needs to decide: the thesis (why), the incentive
    (edge, expected value, max gain) and the risk (max loss, exposure), plus the
    exact order that would be placed on approval.
    """

    id: str
    ts: datetime = Field(default_factory=utcnow)
    match_id: str
    home_team: str
    away_team: str
    minute: int
    score: str
    market_ticker: str
    outcome: Outcome
    action: OrderAction
    # incentive
    model_prob: float
    market_prob: float
    raw_edge: float
    net_edge: float
    expected_value: float  # dollars, ~ contracts * net_edge
    max_gain: float  # dollars if it wins
    max_loss: float  # dollars if it loses (= exposure)
    # the order
    contracts: int
    limit_price_cents: int
    cost_per_contract: float
    exposure_dollars: float
    kelly_fraction: float
    calibration_factor: float
    # narrative + lifecycle
    thesis: str = ""
    risk_note: str = ""
    status: ProposalStatus = ProposalStatus.PENDING
    expires_ts: datetime | None = None
    result: dict[str, Any] | None = None

    @property
    def is_pending(self) -> bool:
        return self.status is ProposalStatus.PENDING


class EdgeSignal(BaseModel):
    """The output of comparing model vs market for one outcome/market."""

    match_id: str
    ts: datetime = Field(default_factory=utcnow)
    outcome: Outcome
    market_ticker: str
    model_prob: float
    market_prob: float  # de-vigged
    market_yes_ask: int | None = None  # cents we'd pay to BUY yes
    market_yes_bid: int | None = None  # cents we'd receive to SELL yes
    raw_edge: float  # model_prob - market_prob
    est_cost: float  # fees + spread + slippage, in probability units
    net_edge: float  # raw_edge magnitude minus cost (signed toward the trade)
    action: OrderAction | None = None  # buy/sell yes, or None
    actionable: bool = False
    reason: str = ""
