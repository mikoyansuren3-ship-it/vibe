"""Shot-based proxy for live xG — used when the data provider omits expected goals.

API-Football's in-play ``/fixtures/statistics`` for the 2026 World Cup returns 12
stats and **no ``expected_goals`` row at all**, so ``TeamStats.xg`` arrives empty.
Treating that as ``0.0`` is actively harmful: the in-play model reads it as "zero
chances created", suppresses the remaining-goal rate, and over-rates the draw. This
module reconstructs a usable xG from the shot counts the feed *does* supply.

Weights were fitted by least squares on 128 StatsBomb World Cup matches (2018+2022),
regressing cumulative real per-shot xG on cumulative shots with no intercept (0 shots
=> 0 xG):

    xG ≈ w_sot·(shots on target) + w_off·(off-target / blocked shots)

Per-tick fit (3028 points): w_sot=0.187, w_off=0.067, R²=0.72. Stable on 255
independent per-match endpoints (0.201 / 0.063, R²=0.63), so the coefficients are
not an autocorrelation artifact. Reproduce with ``scripts/fit_xg_proxy.py``.

This is a *fallback*. A real live-xG feed (the eventual #3 fix) is always preferred:
``observed_xg`` returns the provider's xG whenever it is present and informative.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..models.schemas import TeamStats

# Fitted on 128 StatsBomb WC matches — see module docstring / scripts/fit_xg_proxy.py.
DEFAULT_W_SOT = 0.1865  # xG per shot on target
DEFAULT_W_OFF = 0.0665  # xG per off-target / blocked shot
DEFAULT_W_BIG_CHANCE = 0.0  # reserved; the WC live feed omits "big chances"


def proxy_xg(
    stats: "TeamStats",
    *,
    w_sot: float = DEFAULT_W_SOT,
    w_off: float = DEFAULT_W_OFF,
    w_big_chance: float = DEFAULT_W_BIG_CHANCE,
) -> float | None:
    """Estimate cumulative xG from shot counts, or ``None`` if there is no shot signal.

    Returns ``None`` (rather than ``0.0``) when the team has no shots/SOT/big-chances
    recorded at all — that is indistinguishable from a provider that doesn't track
    shots, so the caller should fall back to the prior rather than assume zero threat.
    """
    shots = stats.shots or 0
    sot = stats.shots_on_target or 0
    big_chances = stats.big_chances or 0
    if shots <= 0 and sot <= 0 and big_chances <= 0:
        return None
    off_target = max(0, shots - sot)
    return w_sot * sot + w_off * off_target + w_big_chance * big_chances


def observed_xg(
    stats: "TeamStats",
    *,
    w_sot: float = DEFAULT_W_SOT,
    w_off: float = DEFAULT_W_OFF,
    w_big_chance: float = DEFAULT_W_BIG_CHANCE,
) -> float | None:
    """Best available live xG: real provider xG → shot proxy → unknown (``None``).

    * Real, positive provider xG wins (the path a true live-xG feed takes).
    * Otherwise use the shot-based proxy. This also covers legacy captures where a
      missing xG was stored as ``0.0`` *with* shots recorded — an impossible combo
      for a real feed, so we treat it as missing and reconstruct from the shots.
    * If there is neither xG nor any shot signal, return the raw value (``None`` for
      a feed that omits xG, or a genuine ``0.0``) so the model can fall back cleanly.
    """
    if stats.xg is not None and stats.xg > 0.0:
        return stats.xg
    proxy = proxy_xg(stats, w_sot=w_sot, w_off=w_off, w_big_chance=w_big_chance)
    if proxy is not None:
        return proxy
    return stats.xg
