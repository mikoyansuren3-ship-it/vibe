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

import numpy as np

from ..models.schemas import MatchPeriod, MatchSnapshot, Probabilities, TeamStats
from .base import ProbabilityModel
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


class ModelConfigLike:
    """Duck-typed config (so the model can be built from the pydantic ModelSection
    or a plain object in tests)."""

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
    def _remaining_rates(self, match: MatchSnapshot) -> tuple[float, float]:
        elapsed = min(max(match.minute, 0), 90)
        rem_min = max(0.0, 90.0 - elapsed)
        home_full, away_full = self._prior_full_rates(match)
        prior_h_pm = home_full / 90.0
        prior_a_pm = away_full / 90.0

        # Observed scoring rate from live xG. When the provider supplies no xG (e.g.
        # API-Football's in-play WC feed) we reconstruct it from shots; when even that
        # is unavailable we fall back to the prior PER SIDE, so a missing signal never
        # masquerades as "zero chances created" (which would over-rate the draw).
        obs_h = self._observed_xg(match.home) if elapsed >= 1 else None
        obs_a = self._observed_xg(match.away) if elapsed >= 1 else None
        obs_h_pm = obs_h / elapsed if obs_h is not None else prior_h_pm
        obs_a_pm = obs_a / elapsed if obs_a is not None else prior_a_pm

        # Live weight grows with minutes played, capped by config.
        w = self.cfg.live_xg_weight * (elapsed / 90.0)
        h_pm = (1 - w) * prior_h_pm + w * obs_h_pm
        a_pm = (1 - w) * prior_a_pm + w * obs_a_pm

        lam = h_pm * rem_min
        mu = a_pm * rem_min

        lam, mu = self._apply_red_cards(match, lam, mu)
        lam, mu = self._apply_game_state(match, lam, mu)
        return lam, mu

    def _apply_red_cards(
        self, match: MatchSnapshot, lam: float, mu: float
    ) -> tuple[float, float]:
        p = self.cfg.red_card_xg_penalty
        boost = 1.0 + (1.0 - p)  # symmetric opponent boost
        if match.home.red_cards:
            lam *= p**match.home.red_cards
            mu *= boost**match.home.red_cards
        if match.away.red_cards:
            mu *= p**match.away.red_cards
            lam *= boost**match.away.red_cards
        return lam, mu

    def _apply_game_state(
        self, match: MatchSnapshot, lam: float, mu: float
    ) -> tuple[float, float]:
        diff = match.score_diff
        if diff > 0:  # home leading
            lam *= self.leader_mult
            mu *= self.chaser_mult
        elif diff < 0:  # away leading
            mu *= self.leader_mult
            lam *= self.chaser_mult
        return lam, mu

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
