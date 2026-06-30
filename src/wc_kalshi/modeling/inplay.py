"""Dixon-Coles in-play 1X2 model (transparent, calibrated baseline).

Models the **full-time (90') result with draws** — the resolvable event for a
group-stage match market. Pipeline per tick:

  1. Prior full-match scoring rates per team, tilted by Elo + home designation.
  2. Project *remaining* goals by blending the prior per-minute rate with the
     observed live-xG per-minute rate; the live weight grows with minutes played
     (early xG is noisy, late xG is informative).
  3. Apply red-card and game-state multipliers to the remaining rates.
  4. Convolve remaining-goal Poissons (with the Dixon-Coles low-score correction)
     and add the current score to get P(home/draw/away).

Every constant below is either config-driven or documented; the *mechanism*
(more time -> more live weight, red card -> rate shock, score added, normalized
to 1) is what the unit tests pin down.
"""

from __future__ import annotations

from typing import Protocol

import numpy as np

from ..models.schemas import MatchPeriod, MatchSnapshot, Probabilities, TeamStats
from .base import ProbabilityModel
from .intensity import credibility_weight, red_card_factors, remaining_fraction, score_state_mults
from .intensity_profile import half_weights
from .poisson import one_x_two, remaining_goal_matrix
from .xg_proxy import DEFAULT_W_BIG_CHANCE, DEFAULT_W_OFF, DEFAULT_W_SOT, observed_xg

# Fallback defaults for the behavioural constants, used only when a duck-typed config
# omits them. The production path reads these from ModelSection (config-driven, fittable
# via modeling/fit.py) — they are no longer hard-coded magic numbers.
_LEADER_MULT = 0.97  # leaders protect (lower remaining rate)
_CHASER_MULT = 1.06  # chasers push (higher remaining rate)
_ELO_TILT = 0.25  # how strongly Elo difference tilts the prior scoring split
# Default rating for a team missing from the Elo table. Used so the Elo tilt still applies
# when ONE side is unrated (e.g. a smaller WC nation), instead of dropping to a flat,
# home-biased prior that badly under-rates the rated favourite. ~ a weak-side baseline.
_DEFAULT_ELO = 1650.0

# Extra-time (knockout) layer — a 30' phase conditional on the score being level at 90'.
_ET_MINUTES = 30.0
_ET_INTENSITY = 0.95  # mild caution/fatigue damp vs a pro-rated 90' scoring rate
_ET_MAX_GOALS = 8


class ModelConfigLike(Protocol):
    """Structural config protocol (so the model accepts the pydantic ModelSection or a
    plain object in tests). A Protocol — not a base class — so ModelSection satisfies it
    by shape without inheriting; behavioural knobs below are read via getattr defaults."""

    base_home_xg: float
    base_away_xg: float
    home_advantage: float
    draw_rho: float
    live_xg_weight: float
    red_card_xg_penalty: float
    max_goals: int
    # Optional (config-driven) behavioural constants; defaults applied if absent.
    elo_tilt: float
    leader_mult: float
    chaser_mult: float


class DixonColesInplayModel(ProbabilityModel):
    name = "dixon_coles_inplay"

    def __init__(self, cfg: ModelConfigLike) -> None:
        self.cfg = cfg
        self.elo_tilt = float(getattr(cfg, "elo_tilt", _ELO_TILT))
        self.leader_mult = float(getattr(cfg, "leader_mult", _LEADER_MULT))
        self.chaser_mult = float(getattr(cfg, "chaser_mult", _CHASER_MULT))
        # Intensity-engine knobs (intensity.py). Defaults reproduce legacy behaviour, so a
        # duck-typed test config that omits them is unchanged.
        self.goal_time_slope = float(getattr(cfg, "goal_time_slope", 0.0))
        self.score_state_per_goal = float(getattr(cfg, "score_state_per_goal", 0.0))
        self.xg_blend_mode = str(getattr(cfg, "xg_blend_mode", "linear"))
        self.xg_info_k = float(getattr(cfg, "xg_info_k", 1.3))
        self.red_card_opponent_boost = getattr(cfg, "red_card_opponent_boost", None)

    # -- priors ---------------------------------------------------------- #
    def _prior_full_rates(self, match: MatchSnapshot) -> tuple[float, float]:
        base_h = self.cfg.base_home_xg
        base_a = self.cfg.base_away_xg
        ctx = match.context
        # Apply the Elo tilt whenever AT LEAST one side is rated: default the unrated side
        # to a weak baseline rather than dropping the tilt entirely (which would let the
        # home/away base split alone decide, badly under-rating a rated favourite who is
        # listed "away" — e.g. Algeria vs an unrated Jordan).
        if ctx and (ctx.home_elo is not None or ctx.away_elo is not None):
            he = ctx.home_elo if ctx.home_elo is not None else _DEFAULT_ELO
            ae = ctx.away_elo if ctx.away_elo is not None else _DEFAULT_ELO
            diff = (he - ae) / 400.0
            tilt = 10 ** (self.elo_tilt * diff)
            home = base_h * tilt
            away = base_a / tilt
        else:
            home, away = base_h, base_a
        # Home-designation advantage only when not a neutral venue.
        if ctx is not None and not ctx.neutral_venue:
            home *= 1.0 + self.cfg.home_advantage
        return max(0.05, home), max(0.05, away)

    def _observed_xg(self, team: TeamStats) -> float | None:
        """Best live xG for one side: real provider xG → shot proxy → unknown.

        Uses the fitted proxy weights from config (falling back to the module
        defaults for duck-typed test configs that omit them).
        """
        return observed_xg(
            team,
            w_sot=float(getattr(self.cfg, "xg_proxy_sot", DEFAULT_W_SOT)),
            w_off=float(getattr(self.cfg, "xg_proxy_off", DEFAULT_W_OFF)),
            w_big_chance=float(getattr(self.cfg, "xg_proxy_big_chance", DEFAULT_W_BIG_CHANCE)),
        )

    # -- remaining-rate projection --------------------------------------- #
    def _blended_per_minute_rates(self, match: MatchSnapshot, elapsed: int) -> tuple[float, float]:
        """Blended (Elo prior + live-xG) per-minute scoring rates, BEFORE the time
        profile, red cards and game-state multipliers. Shared by the 1X2 head and the
        extra-time / first-to-score / team-total heads that need clean per-minute rates.

        When the provider supplies no xG (API-Football's in-play WC feed) it's reconstructed
        from shots; when even that is unavailable we fall back to the prior PER SIDE, so a
        missing signal never masquerades as "zero chances created" (over-rating the draw).
        """
        home_full, away_full = self._prior_full_rates(match)
        prior_h_pm = home_full / 90.0
        prior_a_pm = away_full / 90.0

        obs_h = self._observed_xg(match.home) if elapsed >= 1 else None
        obs_a = self._observed_xg(match.away) if elapsed >= 1 else None
        obs_h_pm = obs_h / elapsed if obs_h is not None else prior_h_pm
        obs_a_pm = obs_a / elapsed if obs_a is not None else prior_a_pm

        if self.xg_blend_mode == "credibility":
            # Info-weighted: trust live xG in proportion to how much has accumulated.
            w_h = credibility_weight(obs_h or 0.0, self.xg_info_k)
            w_a = credibility_weight(obs_a or 0.0, self.xg_info_k)
        else:  # legacy: a single elapsed-driven weight shared by both sides.
            w_h = w_a = self.cfg.live_xg_weight * (elapsed / 90.0)

        h_pm = (1 - w_h) * prior_h_pm + w_h * obs_h_pm
        a_pm = (1 - w_a) * prior_a_pm + w_a * obs_a_pm
        return h_pm, a_pm

    def _remaining_rates(self, match: MatchSnapshot) -> tuple[float, float]:
        elapsed = min(max(match.minute, 0), 90)
        h_pm, a_pm = self._blended_per_minute_rates(match, elapsed)

        # Time-inhomogeneous profile: full-match expected goals × fraction still to come.
        # goal_time_slope=0 reproduces the legacy flat (90-elapsed) projection exactly.
        f_rem = remaining_fraction(elapsed, self.goal_time_slope)
        lam = h_pm * 90.0 * f_rem
        mu = a_pm * 90.0 * f_rem

        lam, mu = self._apply_red_cards(match, lam, mu)
        lam, mu = self._apply_game_state(match, lam, mu)
        return lam, mu

    def remaining_rates(self, match: MatchSnapshot) -> tuple[float, float]:
        """Public ``(λ_rem, μ_rem)`` — the remaining-goal expectations that build ``M`` and
        the 1X2 head (plan P0.1: expose the backbone rates for downstream heads). The SAME
        rates a bespoke head (e.g. first-to-score's competing-Poisson split) must consume to
        stay coherent with the scoreline matrix. Includes red-card + game-state multipliers;
        at a level (0-0) score the game-state factor is the identity, so a first-passage head
        — only un-settled while 0-0 — sees the clean conditional rates."""
        return self._remaining_rates(match)

    def level_game_per_minute_rates(self, match: MatchSnapshot) -> tuple[float, float]:
        """Per-minute scoring rates with the game-state multiplier neutral (score treated as
        level) — the clean conditional rates a FUTURE extra-time / first-to-score / team-total
        head will need (none exist yet; reserved hook, plan P0.1). The caller scales by the
        phase duration and applies red cards itself (red cards carry into extra time)."""
        elapsed = min(max(match.minute, 0), 90)
        return self._blended_per_minute_rates(match, elapsed)

    def _apply_red_cards(
        self, match: MatchSnapshot, lam: float, mu: float
    ) -> tuple[float, float]:
        own, boost = red_card_factors(self.cfg.red_card_xg_penalty, self.red_card_opponent_boost)
        if match.home.red_cards:
            lam *= own**match.home.red_cards
            mu *= boost**match.home.red_cards
        if match.away.red_cards:
            mu *= own**match.away.red_cards
            lam *= boost**match.away.red_cards
        return lam, mu

    def _apply_game_state(
        self, match: MatchSnapshot, lam: float, mu: float
    ) -> tuple[float, float]:
        home_mult, away_mult = score_state_mults(
            match.score_diff,
            leader_mult=self.leader_mult,
            chaser_mult=self.chaser_mult,
            per_goal=self.score_state_per_goal,
        )
        return lam * home_mult, mu * away_mult

    def _effective_rho(self, match: MatchSnapshot) -> float:
        """Effective Dixon-Coles rho applied to the REMAINING-goal matrix.

        The DC tau correction adjusts the *full-time* low-score cells (0-0/1-0/0-1/1-1),
        and it is exact only when the remaining-goal matrix IS the full-time scoreline —
        i.e. at 0-0. Applying the raw correction to remaining goals after a goal has
        been scored is unjustified (remaining 1-1 is no longer full-time 1-1). The
        low-score dependence is also a kickoff/cagey-start phenomenon. So:

          * score 0-0  -> apply rho, faded by the fraction of match remaining;
          * any goals  -> rho = 0 (no spurious correction).
        """
        if match.home_score != 0 or match.away_score != 0:
            return 0.0
        elapsed = min(max(match.minute, 0), 90)
        rem_frac = max(0.0, 90.0 - elapsed) / 90.0
        return self.cfg.draw_rho * rem_frac

    # -- prediction ------------------------------------------------------ #
    def predict(self, match: MatchSnapshot) -> Probabilities:
        # Finished: degenerate on the actual result.
        if match.period is MatchPeriod.FULL_TIME or match.status == "finished":
            d = match.score_diff
            ph, pd, pa = (1.0, 0.0, 0.0) if d > 0 else (0.0, 1.0, 0.0) if d == 0 else (0.0, 0.0, 1.0)
            return Probabilities(
                match_id=match.match_id, p_home=ph, p_draw=pd, p_away=pa, source=self.name
            )

        lam, mu = self._remaining_rates(match)
        rho_eff = self._effective_rho(match)
        ph, pd, pa = one_x_two(
            lam,
            mu,
            current_diff=match.score_diff,
            rho=rho_eff,
            max_goals=self.cfg.max_goals,
        )
        return Probabilities(
            match_id=match.match_id,
            p_home=ph,
            p_draw=pd,
            p_away=pa,
            source=self.name,
            meta={
                "lam_rem": round(lam, 4),
                "mu_rem": round(mu, 4),
                "minute": match.minute,
                "score": f"{match.home_score}-{match.away_score}",
                "net_red_cards": match.net_red_cards,
            },
        ).normalized()

    def scoreline_matrix(self, match: MatchSnapshot) -> np.ndarray:
        """Joint final-score distribution ``M[i, j] = P(home_final=i, away_final=j)``.

        The basis for every scoreline-derived market (total / spread / BTTS / correct-score
        / team-total / margin — see modeling/derived.py). Uses the SAME remaining-goal
        matrix ``predict`` collapses into 1X2, shifted by the current score.
        """
        hs, as_ = match.home_score, match.away_score
        if match.period is MatchPeriod.FULL_TIME or match.status == "finished":
            m = np.zeros((hs + 1, as_ + 1))
            m[hs, as_] = 1.0  # degenerate on the actual result
            return m
        lam, mu = self._remaining_rates(match)
        rem = remaining_goal_matrix(
            lam, mu, rho=self._effective_rho(match), max_goals=self.cfg.max_goals
        )
        n = rem.shape[0]
        m = np.zeros((n + hs, n + as_))
        m[hs : hs + n, as_ : as_ + n] = rem  # shift remaining goals onto the current score
        return m

    def scoreline_matrix_et(self, match: MatchSnapshot) -> np.ndarray:
        """Extra-time (30') joint score matrix ``M_ET[i,j] = P(home scores i, away j in ET)``,
        CONDITIONAL on the score being level at 90'. Uses the level-game per-minute rates
        (game-state neutral) × 30' × a mild fatigue/caution damp, with red cards carried over
        (a sent-off player stays off in ET). ``rho=0`` — the Dixon-Coles low-score correction
        is a kickoff / full-time-0-0 artifact, not justified on a fresh 30' restart. Knockout
        only; the caller composes it with the regulation matrix (see modeling/knockout.py)."""
        h_pm, a_pm = self.level_game_per_minute_rates(match)
        lam = h_pm * _ET_MINUTES * _ET_INTENSITY
        mu = a_pm * _ET_MINUTES * _ET_INTENSITY
        lam, mu = self._apply_red_cards(match, lam, mu)  # red cards carry into extra time
        return remaining_goal_matrix(lam, mu, rho=0.0, max_goals=_ET_MAX_GOALS)

    def half_scoreline_matrices(self, match: MatchSnapshot) -> tuple[np.ndarray, np.ndarray]:
        """(M_1h, M_2h) per-half joint score matrices, splitting the full-match remaining
        rates by the within-match intensity profile. ``conv(M_1h, M_2h)`` reconstructs the
        full-time remaining-goal matrix (Poisson additivity), so the half markets stay
        coherent with the full-match board. ``rho=0``: the low-score correction is a
        full-time artifact, not a per-half one. Intended for an UPCOMING (pre-kickoff) game;
        an in-play game would condition on the current half + score (not handled here)."""
        lam, mu = self._remaining_rates(match)
        w1, w2 = half_weights()
        mg = self.cfg.max_goals
        m1 = remaining_goal_matrix(lam * w1, mu * w1, rho=0.0, max_goals=mg)
        m2 = remaining_goal_matrix(lam * w2, mu * w2, rho=0.0, max_goals=mg)
        return m1, m2
