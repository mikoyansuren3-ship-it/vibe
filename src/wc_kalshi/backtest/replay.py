"""Backtest / replay harness.

Two entry points, both running the SAME ``TickProcessor`` the live system uses:

  * ``run_synthetic`` — mint N simulated matches (no API keys, deterministic) and
    evaluate the strategy. This is the CI / sample-data path.
  * ``run_replay``    — re-feed stored ``MatchSnapshot``/``MarketSnapshot`` rows from
    a previous live/demo session, so a real captured run can be re-evaluated.

Reports realized P&L, fees, gross edge, fill count, an equity curve, and
calibration (Brier / log-loss / ECE + reliability table).
"""

from __future__ import annotations

import os
import random
import statistics
import tempfile
from dataclasses import dataclass, field
from typing import Any

from ..config import AppConfig, RunMode
from ..engine.builders import Runtime, build_runtime
from ..engine.match_loop import MatchState, TickProcessor
from ..ingestion.football.simulated import FIXTURES, simulate_full_match
from ..logging_setup import get_logger
from ..models.db import Database
from ..models.schemas import MarketSnapshot, MatchSnapshot

log = get_logger("backtest")


def bootstrap_ci(
    samples: list[float], *, iters: int = 2000, alpha: float = 0.05, seed: int = 0
) -> tuple[float, float]:
    """Percentile bootstrap CI for the MEAN of ``samples``.

    Per-match P&L is serially dependent (compounding) and heteroskedastic, so the
    Gaussian t-stat is invalid. Resampling matches with replacement gives an honest
    interval on mean per-match P&L without that i.i.d. assumption.
    """
    n = len(samples)
    if n < 2:
        return (0.0, 0.0)
    rng = random.Random(seed)
    means: list[float] = []
    for _ in range(iters):
        resampled = [samples[rng.randrange(n)] for _ in range(n)]
        means.append(sum(resampled) / n)
    means.sort()
    lo = means[int((alpha / 2) * iters)]
    hi = means[min(iters - 1, int((1 - alpha / 2) * iters))]
    return (lo, hi)


@dataclass
class BacktestResult:
    n_matches: int = 0
    n_fills: int = 0
    realized_pnl: float = 0.0
    fees_paid: float = 0.0
    starting_bankroll: float = 0.0
    ending_equity: float = 0.0
    per_match_pnl: list[float] = field(default_factory=list)
    equity_curve: list[float] = field(default_factory=list)
    calibration: dict[str, float] = field(default_factory=dict)
    reliability: list[dict[str, float]] = field(default_factory=list)
    stake_mode: str = "kelly"  # "kelly" (compounding) | "fixed" (constant stake)
    avg_clv: float = 0.0  # mean closing-line value per contract (probability units)
    clv_n: int = 0  # number of fills with a usable closing price
    pnl_ci: tuple[float, float] = (0.0, 0.0)  # bootstrap CI for mean per-match P&L

    @property
    def gross_pnl(self) -> float:
        return self.realized_pnl + self.fees_paid

    @property
    def roi(self) -> float:
        return self.realized_pnl / self.starting_bankroll if self.starting_bankroll else 0.0

    @property
    def t_stat(self) -> float:
        """NOTE: only meaningful in fixed-stake mode. Under Kelly compounding the
        per-match samples are serially dependent, so prefer the bootstrap CI + CLV."""
        if len(self.per_match_pnl) < 2:
            return 0.0
        sd = statistics.pstdev(self.per_match_pnl)
        if sd == 0:
            return 0.0
        return statistics.mean(self.per_match_pnl) / (sd / len(self.per_match_pnl) ** 0.5)

    def to_dict(self) -> dict[str, Any]:
        return {
            "n_matches": self.n_matches,
            "n_fills": self.n_fills,
            "stake_mode": self.stake_mode,
            "realized_pnl": round(self.realized_pnl, 2),
            "fees_paid": round(self.fees_paid, 2),
            "gross_pnl": round(self.gross_pnl, 2),
            "roi": round(self.roi, 4),
            "t_stat": round(self.t_stat, 2),
            "pnl_ci": [round(self.pnl_ci[0], 3), round(self.pnl_ci[1], 3)],
            "avg_clv": round(self.avg_clv, 4),
            "clv_n": self.clv_n,
            "starting_bankroll": self.starting_bankroll,
            "ending_equity": round(self.ending_equity, 2),
            "calibration": {k: round(v, 4) for k, v in self.calibration.items()},
        }

    def report(self) -> str:
        c = self.calibration
        lines = [
            "=" * 60,
            "BACKTEST REPORT",
            "=" * 60,
            f"  matches:            {self.n_matches}",
            f"  fills:              {self.n_fills}",
            f"  starting bankroll:  {self.starting_bankroll:.2f}",
            f"  ending equity:      {self.ending_equity:.2f}",
            f"  realized P&L:       {self.realized_pnl:+.2f}",
            f"  fees paid:          {self.fees_paid:.2f}",
            f"  gross P&L (pre-fee):{self.gross_pnl:+.2f}",
            f"  ROI:                {self.roi*100:+.2f}%",
            f"  stake mode:         {self.stake_mode}",
            f"  per-match t-stat:   {self.t_stat:.2f}  ({'valid' if self.stake_mode == 'fixed' else 'IGNORE: compounding'})",
            f"  per-match mean 95% CI: [{self.pnl_ci[0]:+.3f}, {self.pnl_ci[1]:+.3f}]  (bootstrap)",
            f"  avg CLV/contract:   {self.avg_clv:+.4f}  over {self.clv_n} fills  (edge vs closing line)",
            "-" * 60,
            "  CALIBRATION",
            f"  Brier:    {c.get('brier', float('nan')):.4f}  (uniform 1X2 ~= 0.667)",
            f"  LogLoss:  {c.get('log_loss', float('nan')):.4f}  (uniform ~= 1.099)",
            f"  ECE:      {c.get('ece', float('nan')):.4f}",
            f"  Kelly calibration factor: {c.get('calibration_factor', float('nan')):.2f}",
            "-" * 60,
            "  RELIABILITY (pooled 1X2 bins)",
            f"  {'pred':>8} {'empirical':>10} {'count':>7}",
        ]
        for row in self.reliability:
            lines.append(
                f"  {row['mean_predicted']:>8.3f} {row['empirical_freq']:>10.3f} {int(row['count']):>7}"
            )
        lines.append("=" * 60)
        return "\n".join(lines)


class Backtester:
    def __init__(
        self,
        cfg: AppConfig,
        *,
        trade: bool = True,
        db: Database | None = None,
        stake_mode: str = "kelly",
        fixed_stake: float | None = None,
    ) -> None:
        # Backtests always run in paper mode (no keys / no exchange) and in an
        # isolated DB so they never touch live data.
        self.cfg = cfg.model_copy(deep=True)
        self.cfg.mode = RunMode.PAPER
        # Disable the daily-loss halt for evaluation runs (we want the full sample);
        # the guardrail itself is tested separately.
        if db is None:
            fd, path = tempfile.mkstemp(prefix="wck-backtest-", suffix=".sqlite3")
            os.close(fd)
            db = Database(f"sqlite:///{path}")
        self.rt: Runtime = build_runtime(self.cfg, db=db)
        self.rt.audit.enabled = False  # backtests don't need the audit trail (speed)
        # Fixed-stake mode makes per-match P&L (closer to) i.i.d. for honest stats.
        self.stake_mode = stake_mode
        if stake_mode == "fixed":
            self.rt.sizer.fixed_stake = (
                fixed_stake if fixed_stake is not None else 0.02 * self.cfg.risk.starting_bankroll
            )
        # backtests are always autonomous (no human in the loop to approve proposals)
        self.processor = TickProcessor(
            self.rt, trade=trade, persist=False, decision_mode="autonomous"
        )

    def _collect(self, per_match_pnl: list[float], equity_curve: list[float]) -> BacktestResult:
        rt = self.rt
        avg_clv, clv_n = self._compute_clv()
        return BacktestResult(
            n_matches=len(per_match_pnl),
            n_fills=0,  # set by caller
            realized_pnl=rt.portfolio.realized_pnl,
            fees_paid=rt.portfolio.fees_paid,
            starting_bankroll=rt.portfolio.starting_bankroll,
            ending_equity=rt.portfolio.equity(rt.last_mids),
            per_match_pnl=per_match_pnl,
            equity_curve=equity_curve,
            calibration=rt.calibration.metrics(),
            reliability=rt.calibration.reliability_table(),
            stake_mode=self.stake_mode,
            avg_clv=avg_clv,
            clv_n=clv_n,
            pnl_ci=bootstrap_ci(per_match_pnl),
        )

    def _compute_clv(self) -> tuple[float, float]:
        """Mean closing-line value per contract: how much better we entered than the
        market's CLOSING mid for that outcome. Positive = we beat the close (the gold
        standard for whether an edge is real, independent of lucky settlement)."""
        rt = self.rt
        clvs: list[float] = []
        for f in rt.fills_log:
            close_mid = rt.last_mids.get(f["market_ticker"])
            if close_mid is None:
                continue
            entry = f["entry_price_cents"] / 100.0
            if f["action"] == "buy":  # bought Yes: good if close ended higher
                clvs.append(close_mid - entry)
            else:  # sold Yes: good if close ended lower
                clvs.append(entry - close_mid)
        if not clvs:
            return 0.0, 0
        return sum(clvs) / len(clvs), len(clvs)

    async def run_synthetic(
        self, *, n_matches: int = 100, seed0: int = 0, halt_disabled: bool = True
    ) -> BacktestResult:
        rt = self.rt
        if halt_disabled:
            rt.risk.limits.max_daily_loss = 1e12
        per_match: list[float] = []
        equity_curve: list[float] = []
        n_fills = 0
        prev_realized = 0.0
        for i in range(n_matches):
            seed = seed0 + i
            snaps = simulate_full_match(
                seed=seed, fixture=FIXTURES[seed % len(FIXTURES)], match_id=f"bt-{seed}"
            )
            n_fills += await self._run_match(snaps)
            delta = rt.portfolio.realized_pnl - prev_realized
            prev_realized = rt.portfolio.realized_pnl
            per_match.append(delta)
            equity_curve.append(rt.portfolio.equity(rt.last_mids))
        result = self._collect(per_match, equity_curve)
        result.n_fills = n_fills
        return result

    async def run_replay(
        self, source_db: Database, *, match_ids: list[str] | None = None
    ) -> BacktestResult:
        """Replay stored snapshots from a previous session through the strategy."""
        ids = match_ids or source_db.match_ids()
        per_match: list[float] = []
        equity_curve: list[float] = []
        n_fills = 0
        prev_realized = 0.0
        for match_id in ids:
            match_snaps = source_db.iter_match_snapshots(match_id)
            market_snaps = source_db.iter_market_snapshots(match_id)
            ticks = _bucket_market_by_tick(match_snaps, market_snaps)
            st = MatchState(match_id)
            for match, mk in ticks:
                summary = await self.processor.process(match, mk, st)  # noqa: F841
            n_fills += st.n_fills
            delta = self.rt.portfolio.realized_pnl - prev_realized
            prev_realized = self.rt.portfolio.realized_pnl
            per_match.append(delta)
            equity_curve.append(self.rt.portfolio.equity(self.rt.last_mids))
        result = self._collect(per_match, equity_curve)
        result.n_fills = n_fills
        return result

    async def run_historical(self, matches: list) -> BacktestResult:
        """Replay REAL match timelines (xG + recorded market prices) through the
        strategy. ``matches`` is a list of matches, each a list of
        ``(MatchSnapshot, [MarketSnapshot])`` ticks (see backtest/historical.py).

        Degrades gracefully: ticks without market prices simply produce no edges, so a
        match with no prices anywhere still scores model calibration on its real result.
        """
        from .historical import has_market_data

        rt = self.rt
        rt.risk.limits.max_daily_loss = 1e12  # full sample for evaluation
        if not has_market_data(matches):
            log.warning(
                "historical data has NO market prices: running xG-only calibration "
                "(model accuracy on real outcomes); market edge cannot be measured."
            )
        per_match: list[float] = []
        equity_curve: list[float] = []
        n_fills = 0
        prev_realized = 0.0
        for ticks in matches:
            if not ticks:
                continue
            st = MatchState(ticks[0][0].match_id)
            for match, mk in ticks:
                await self.processor.process(match, mk, st)
            n_fills += st.n_fills
            delta = rt.portfolio.realized_pnl - prev_realized
            prev_realized = rt.portfolio.realized_pnl
            per_match.append(delta)
            equity_curve.append(rt.portfolio.equity(rt.last_mids))
        result = self._collect(per_match, equity_curve)
        result.n_fills = n_fills
        return result

    async def _run_match(self, snaps: list[MatchSnapshot]) -> int:
        st = MatchState(snaps[0].match_id)
        for m in snaps:
            mk = await self.rt.market_feed.snapshots_for_match(m)
            await self.processor.process(m, mk, st)
        return st.n_fills

    async def aclose(self) -> None:
        await self.rt.aclose()


def _bucket_market_by_tick(
    match_snaps: list[MatchSnapshot], market_snaps: list[MarketSnapshot]
) -> list[tuple[MatchSnapshot, list[MarketSnapshot]]]:
    """Pair each match snapshot with the market snapshots captured at that tick.

    Market snapshots for tick *i* were written just after match snapshot *i*, so
    they fall in ``[ts_i, ts_{i+1})``.
    """
    out: list[tuple[MatchSnapshot, list[MarketSnapshot]]] = []
    for i, match in enumerate(match_snaps):
        lo = match.ts
        hi = match_snaps[i + 1].ts if i + 1 < len(match_snaps) else None
        bucket = [
            s for s in market_snaps if s.ts >= lo and (hi is None or s.ts < hi)
        ]
        out.append((match, bucket))
    return out
