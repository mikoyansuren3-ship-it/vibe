"""Deterministic in-play match simulator — the DEFAULT football feed.

Lets the entire pipeline (features -> model -> edge -> sizing -> execution ->
dashboard) run end-to-end with **no API keys and no quota burn**. The engine
produces coherent, varied matches: goals follow the teams' strength-derived
scoring rates, xG accrues from simulated shots, and rare shocks (red cards) fire
so the in-play model and guardrails are genuinely exercised.

The same ``SimMatch`` engine powers both the live provider (incremental) and the
``simulate_full_match`` helper the backtest harness uses to mint complete games.
"""

from __future__ import annotations

import random
from datetime import timedelta

from ...models.schemas import MatchContext, MatchPeriod, MatchSnapshot, TeamStats
from ...util import utcnow
from .base import FootballDataProvider

# Illustrative pre-match priors (World Football Elo-ish, FIFA rank, squad value €m).
# Documented as illustrative — swap in a real source for production priors.
TEAM_PRIORS: dict[str, tuple[float, int, float]] = {
    "Spain": (2090, 2, 1180),
    "France": (2070, 3, 1130),
    "Argentina": (2065, 1, 730),
    "England": (2010, 4, 1380),
    "Brazil": (2040, 5, 1050),
    "Germany": (1990, 9, 980),
    "Portugal": (1985, 7, 1020),
    "Netherlands": (1975, 6, 760),
    "USA": (1815, 16, 360),
    "Mexico": (1790, 13, 240),
    "Croatia": (1900, 10, 430),
    "Morocco": (1860, 12, 320),
    "Japan": (1820, 18, 280),
    "Wales": (1760, 30, 210),
    "Canada": (1760, 32, 190),
    "Australia": (1730, 25, 160),
    "Ghana": (1690, 60, 230),
    "Ecuador": (1760, 28, 200),
}
_DEFAULT_PRIOR = (1750, 40, 200.0)

FIXTURES: list[tuple[str, str]] = [
    ("Spain", "Germany"),
    ("USA", "Wales"),
    ("Brazil", "Mexico"),
    ("Argentina", "Canada"),
    ("England", "Morocco"),
]


def _prior(team: str) -> tuple[float, int, float]:
    return TEAM_PRIORS.get(team, _DEFAULT_PRIOR)


class SimMatch:
    """A single simulated match with a minute-resolution state machine."""

    def __init__(
        self,
        match_id: str,
        home_team: str,
        away_team: str,
        rng: random.Random,
        *,
        minutes_per_tick: float = 1.0,
    ) -> None:
        self.match_id = match_id
        self.home_team = home_team
        self.away_team = away_team
        self.rng = rng
        self.minutes_per_tick = minutes_per_tick

        home_elo, home_rank, home_val = _prior(home_team)
        away_elo, away_rank, away_val = _prior(away_team)

        # Expected goals over 90' derived from Elo difference around a 2.6-goal game.
        diff = (home_elo - away_elo) / 400.0
        total = 2.6
        home_share = 1.0 / (1.0 + 10 ** (-diff)) * 0.55 + 0.25  # home edge baked in
        base_home = max(0.35, total * home_share)
        base_away = max(0.30, total * (1.0 - home_share))

        # Hidden "on-the-day form": a per-team multiplier the pre-match Elo market
        # does NOT know about. The true scoring rates (and thus xG) reflect it, so a
        # model that reads live xG can recover this information and gain a real edge
        # over a market that only prices Elo + score + time. Without this, live xG
        # would be pure noise on top of Elo and chasing it would be -EV.
        form_home = min(1.9, max(0.5, 2.718281828 ** rng.gauss(0.0, 0.32)))
        form_away = min(1.9, max(0.5, 2.718281828 ** rng.gauss(0.0, 0.32)))
        self.home_rate = base_home * form_home
        self.away_rate = base_away * form_away

        self.context = MatchContext(
            neutral_venue=True,
            venue="Simulated Stadium",
            home_elo=home_elo,
            away_elo=away_elo,
            home_fifa_rank=home_rank,
            away_fifa_rank=away_rank,
            home_rest_days=float(rng.randint(3, 6)),
            away_rest_days=float(rng.randint(3, 6)),
            home_market_value_m=home_val,
            away_market_value_m=away_val,
            temp_c=float(rng.randint(24, 35)),  # US summer heat
            humidity_pct=float(rng.randint(40, 80)),
        )

        self.minute = 0
        self.home_score = 0
        self.away_score = 0
        self.home = TeamStats(possession=0.5)
        self.away = TeamStats(possession=0.5)
        self.period = MatchPeriod.PRE
        self.finished = False

    # -- dynamics -------------------------------------------------------- #
    def _eff_rates(self) -> tuple[float, float]:
        h, a = self.home_rate, self.away_rate
        # Red cards: the short-handed team scores less and concedes more.
        if self.home.red_cards:
            h *= 0.62 ** self.home.red_cards
            a *= 1.18 ** self.home.red_cards
        if self.away.red_cards:
            a *= 0.62 ** self.away.red_cards
            h *= 1.18 ** self.away.red_cards
        return h, a

    def _step_minute(self) -> None:
        self.minute += 1
        h_rate, a_rate = self._eff_rates()
        for team, rate in ((self.home, h_rate), (self.away, a_rate)):
            lam = rate / 90.0
            # Shot occurrence. A goal IS a converted shot, so xG (sum of shot
            # qualities) tracks expected goals and goals follow shot quality.
            # Scaled to ~10-13 shots and ~xG≈rate over 90'.
            if self.rng.random() < lam * 7.0:
                team.shots += 1
                team.dangerous_attacks += 1
                quality = min(0.97, self.rng.betavariate(1.6, 9.0))  # mean ~0.15
                team.xg = (team.xg or 0.0) + quality  # xg defaults to None now
                on_target = self.rng.random() < (0.30 + quality * 0.5)
                if on_target:
                    team.shots_on_target += 1
                if quality > 0.30:
                    team.big_chances += 1
                # Conversion probability ~ chance quality.
                if self.rng.random() < quality:
                    if not on_target:
                        team.shots_on_target += 1
                    team.big_chances += 1
                    if team is self.home:
                        self.home_score += 1
                    else:
                        self.away_score += 1
            # Set pieces / fouls / offsides drift.
            if self.rng.random() < 0.10:
                team.corners += 1
            if self.rng.random() < 0.18:
                team.fouls += 1
            if self.rng.random() < 0.05:
                team.offsides += 1
            if self.rng.random() < 0.012:
                team.yellow_cards += 1
            # Red card shock: rare.
            if self.rng.random() < 0.00065:
                team.red_cards += 1

        # Possession random walk anchored to relative scoring rate.
        anchor = h_rate / (h_rate + a_rate)
        self.home.possession += (anchor - self.home.possession) * 0.05
        self.home.possession += (self.rng.random() - 0.5) * 0.03
        self.home.possession = min(0.80, max(0.20, self.home.possession))
        self.away.possession = 1.0 - self.home.possession
        # Pass accuracy tracks possession loosely.
        self.home.pass_accuracy = min(0.93, 0.70 + self.home.possession * 0.2)
        self.away.pass_accuracy = min(0.93, 0.70 + self.away.possession * 0.2)

    def advance(self) -> None:
        if self.finished:
            return
        if self.period is MatchPeriod.PRE:
            self.period = MatchPeriod.FIRST_HALF
            self.context = self.context  # kickoff
        steps = max(1, int(round(self.minutes_per_tick)))
        for _ in range(steps):
            if self.minute >= 90:
                break
            self._step_minute()
        if self.minute >= 90:
            self.period = MatchPeriod.FULL_TIME
            self.finished = True
        elif self.minute >= 45:
            self.period = MatchPeriod.SECOND_HALF
        else:
            self.period = MatchPeriod.FIRST_HALF

    def snapshot(self) -> MatchSnapshot:
        status = (
            "finished"
            if self.finished
            else ("live" if self.period.is_live else "scheduled")
        )
        return MatchSnapshot(
            match_id=self.match_id,
            provider="simulated",
            home_team=self.home_team,
            away_team=self.away_team,
            minute=self.minute,
            period=self.period,
            home_score=self.home_score,
            away_score=self.away_score,
            home=self.home.model_copy(deep=True),
            away=self.away.model_copy(deep=True),
            status=status,
            context=self.context,
        )


class SimulatedFootballProvider(FootballDataProvider):
    name = "simulated"

    def __init__(
        self,
        *,
        seed: int = 42,
        num_matches: int = 3,
        minutes_per_tick: float = 1.0,
    ) -> None:
        self.seed = seed
        self.minutes_per_tick = minutes_per_tick
        self.num_matches = max(1, min(num_matches, len(FIXTURES)))
        self.rng = random.Random(seed)
        fixtures = FIXTURES[: self.num_matches]
        self.matches: list[SimMatch] = [
            SimMatch(
                match_id=f"sim-{i+1}",
                home_team=h,
                away_team=a,
                rng=random.Random(seed * 100 + i),
                minutes_per_tick=minutes_per_tick,
            )
            for i, (h, a) in enumerate(fixtures)
        ]

    async def fetch_live(self) -> list[MatchSnapshot]:
        out: list[MatchSnapshot] = []
        for m in self.matches:
            was_finished = m.finished
            if not m.finished:
                m.advance()
            # Emit while live, plus the single final snapshot on the tick it ends.
            if m.period.is_live or (m.finished and not was_finished):
                out.append(m.snapshot())
        return out

    async def fetch_upcoming(self, limit: int = 8) -> list[MatchSnapshot]:
        """PRE-period projections for fixtures NOT in the live window, each with a
        synthetic staggered kickoff (the offline simulator has no real schedule).

        Built fresh and never advanced, so every snapshot stays in ``MatchPeriod.PRE``
        (``status="scheduled"``) — the model degenerates to its Elo-only prior. Ids are
        ``sim-up-*`` so they never collide with the ``sim-*`` live/recorded matches."""
        pending = FIXTURES[self.num_matches:] or FIXTURES
        base = utcnow() + timedelta(hours=6)
        out: list[MatchSnapshot] = []
        for i, (h, a) in enumerate(pending[: max(0, limit)]):
            m = SimMatch(
                match_id=f"sim-up-{i+1}",
                home_team=h,
                away_team=a,
                rng=random.Random(self.seed * 1000 + 900 + i),
                minutes_per_tick=self.minutes_per_tick,
            )
            if m.context is not None:
                m.context.kickoff = base + timedelta(hours=3 * i)
            out.append(m.snapshot())
        return out

    @property
    def all_finished(self) -> bool:
        return all(m.finished for m in self.matches)


def simulate_full_match(
    seed: int,
    fixture: tuple[str, str] | None = None,
    match_id: str = "sim-bt",
) -> list[MatchSnapshot]:
    """Generate a complete 0..90' timeline for one match (used by the backtest)."""
    rng = random.Random(seed)
    h, a = fixture or FIXTURES[seed % len(FIXTURES)]
    m = SimMatch(match_id, h, a, rng, minutes_per_tick=1.0)
    snaps: list[MatchSnapshot] = []
    # kickoff snapshot
    m.period = MatchPeriod.FIRST_HALF
    snaps.append(m.snapshot())
    while not m.finished:
        m.advance()
        snaps.append(m.snapshot())
    return snaps
