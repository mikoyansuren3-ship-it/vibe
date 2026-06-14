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

from ..models.schemas import MatchPeriod, MatchSnapshot, Probabilities
from .base import ProbabilityModel
from .poisson import one_x_two

# Game-state nudges (mild, capped): leaders protect, chasers push.
_LEADER_MULT = 0.97
_CHASER_MULT = 1.06
# How strongly Elo difference tilts the prior scoring split.
_ELO_TILT = 0.25


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


class DixonColesInplayModel(ProbabilityModel):
    name = "dixon_coles_inplay"

    def __init__(self, cfg: ModelConfigLike) -> None:
        self.cfg = cfg

    # -- priors ---------------------------------------------------------- #
    def _prior_full_rates(self, match: MatchSnapshot) -> tuple[float, float]:
        base_h = self.cfg.base_home_xg
        base_a = self.cfg.base_away_xg
        ctx = match.context
        if ctx and ctx.home_elo is not None and ctx.away_elo is not None:
            diff = (ctx.home_elo - ctx.away_elo) / 400.0
            tilt = 10 ** (_ELO_TILT * diff)
            home = base_h * tilt
            away = base_a / tilt
        else:
            home, away = base_h, base_a
        # Home-designation advantage only when not a neutral venue.
        if ctx is not None and not ctx.neutral_venue:
            home *= 1.0 + self.cfg.home_advantage
        return max(0.05, home), max(0.05, away)

    # -- remaining-rate projection --------------------------------------- #
    def _remaining_rates(self, match: MatchSnapshot) -> tuple[float, float]:
        elapsed = min(max(match.minute, 0), 90)
        rem_min = max(0.0, 90.0 - elapsed)
        home_full, away_full = self._prior_full_rates(match)
        prior_h_pm = home_full / 90.0
        prior_a_pm = away_full / 90.0

        if elapsed >= 1:
            obs_h_pm = match.home.xg / elapsed
            obs_a_pm = match.away.xg / elapsed
        else:
            obs_h_pm, obs_a_pm = prior_h_pm, prior_a_pm

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
            lam *= _LEADER_MULT
            mu *= _CHASER_MULT
        elif diff < 0:  # away leading
            mu *= _LEADER_MULT
            lam *= _CHASER_MULT
        return lam, mu

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
        ph, pd, pa = one_x_two(
            lam,
            mu,
            current_diff=match.score_diff,
            rho=self.cfg.draw_rho,
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
