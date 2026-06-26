"""Derive a flat feature vector from a ``MatchSnapshot``.

The Dixon-Coles baseline reads the snapshot directly, but this is the documented
"feature store" layer: it produces the engineered variables an ML model would
consume, and the values are logged for audit and shown on the dashboard.
"""

from __future__ import annotations

from ..models.schemas import MatchSnapshot
from ..modeling.xg_proxy import observed_xg


def match_features(match: MatchSnapshot) -> dict[str, float]:
    elapsed = max(1, min(match.minute, 90))
    ctx = match.context
    home, away = match.home, match.away
    # Observed xG (real if supplied, else shot-based proxy, else 0.0 when unknown).
    hx = observed_xg(home) or 0.0
    ax = observed_xg(away) or 0.0
    feats: dict[str, float] = {
        "minute": float(match.minute),
        "minutes_remaining": match.minutes_remaining,
        "time_fraction_played": min(1.0, match.minute / 90.0),
        "score_diff": float(match.score_diff),
        "total_goals": float(match.home_score + match.away_score),
        "xg_home": hx,
        "xg_away": ax,
        "xg_diff": hx - ax,
        "xg_rate_home": hx / elapsed,
        "xg_rate_away": ax / elapsed,
        "xg_minus_goals_home": hx - match.home_score,
        "xg_minus_goals_away": ax - match.away_score,
        "shots_diff": float(home.shots - away.shots),
        "sot_diff": float(home.shots_on_target - away.shots_on_target),
        "big_chance_diff": float(home.big_chances - away.big_chances),
        "possession_home": home.possession,
        "dangerous_attacks_diff": float(home.dangerous_attacks - away.dangerous_attacks),
        "corners_diff": float(home.corners - away.corners),
        "net_red_cards": float(match.net_red_cards),
        "red_cards_home": float(home.red_cards),
        "red_cards_away": float(away.red_cards),
    }
    if ctx:
        if ctx.home_elo is not None and ctx.away_elo is not None:
            feats["elo_diff"] = ctx.home_elo - ctx.away_elo
        if ctx.home_fifa_rank is not None and ctx.away_fifa_rank is not None:
            feats["fifa_rank_diff"] = float(ctx.away_fifa_rank - ctx.home_fifa_rank)
        if ctx.home_rest_days is not None and ctx.away_rest_days is not None:
            feats["rest_diff"] = ctx.home_rest_days - ctx.away_rest_days
        if ctx.temp_c is not None:
            feats["temp_c"] = ctx.temp_c
        if ctx.humidity_pct is not None:
            feats["humidity_pct"] = ctx.humidity_pct
    # Simple momentum proxy: recent xG dominance scaled by pressure.
    feats["momentum"] = (hx - ax) + 0.1 * (home.shots_on_target - away.shots_on_target)
    return feats
