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

    @property
    def gross_pnl(self) -> float:
        return self.realized_pnl + self.fees_paid

    @property
    def roi(self) -> float:
        return self.realized_pnl / self.starting_bankroll if self.starting_bankroll else 0.0

    @property
    def t_stat(self) -> float:
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
            "realized_pnl": round(self.realized_pnl, 2),
            "fees_paid": round(self.fees_paid, 2),
            "gross_pnl": round(self.gross_pnl, 2),
            "roi": round(self.roi, 4),
            "t_stat": round(self.t_stat, 2),
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
            f"  per-match t-stat:   {self.t_stat:.2f}",
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
    def __init__(self, cfg: AppConfig, *, trade: bool = True, db: Database | None = None) -> None:
        # Backtests always run in paper mode (no keys / no exchange) and in an
        # isolated DB so they never touch live data.
        self.cfg = cfg.model_copy(deep=True)
        self.cfg.mode = RunMode.PAPER
        # Disable the daily-loss halt for evaluation runs (we want the full sample);
        # the guardrail itself is tested separately.
        if db is None:
            path = tempfile.mktemp(prefix="wck-backtest-", suffix=".sqlite3")
            db = Database(f"sqlite:///{path}")
        self.rt: Runtime = build_runtime(self.cfg, db=db)
        self.rt.audit.enabled = False  # backtests don't need the audit trail (speed)
        # backtests are always autonomous (no human in the loop to approve proposals)
        self.processor = TickProcessor(
            self.rt, trade=trade, persist=False, decision_mode="autonomous"
        )

    def _collect(self, per_match_pnl: list[float], equity_curve: list[float]) -> BacktestResult:
        rt = self.rt
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
        )

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
