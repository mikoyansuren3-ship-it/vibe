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


def clustered_clv_ci(
    by_match: dict[str, list[float]], *, iters: int = 5000, alpha: float = 0.05, seed: int = 0
) -> tuple[float, float, int]:
    """Match-CLUSTERED percentile-bootstrap CI for the pooled mean CLV.

    Fills within one match share an outcome, an opening line, and one model
    trajectory, so they are NOT independent — the naive fill-level interval
    overstates precision by the design effect (1 + (m̄−1)·ρ). This resamples whole
    MATCHES with replacement and re-pools their fills each draw, so the interval is
    honest about the real (cluster) sample size. The centre matches the published
    fill-pooled mean. Returns ``(lo, hi, n_clusters)``.
    """
    matches = [m for m, v in by_match.items() if v]
    n = len(matches)
    if n < 2:
        return (0.0, 0.0, n)
    rng = random.Random(seed)
    means: list[float] = []
    for _ in range(iters):
        total = 0.0
        count = 0
        for _ in range(n):
            vals = by_match[matches[rng.randrange(n)]]
            total += sum(vals)
            count += len(vals)
        means.append(total / count if count else 0.0)
    means.sort()
    lo = means[int((alpha / 2) * iters)]
    hi = means[min(iters - 1, int((1 - alpha / 2) * iters))]
    return (lo, hi, n)


def edge_verdict(ci: tuple[float, float]) -> str:
    """Headline verdict from a CLV interval. The honest default when the interval
    brackets zero is "no demonstrated edge" — never "is −EV" or "has edge"."""
    lo, hi = ci
    if lo <= 0.0 <= hi:
        return "indistinguishable_from_zero"
    return "negative" if hi < 0.0 else "positive"


class DataSource:
    """The four metric namespaces. CLV/edge from real Kalshi quotes, a synthetic market,
    StatsBomb calibration (no prices at all), and Betfair lines are DIFFERENT measurements
    on DIFFERENT data — they must never be pooled or averaged together. Every result is
    tagged so the UI/manifest can't silently conflate them (see ``require_same_source``)."""

    SYNTHETIC = "synthetic"  # simulated matches + simulated market — CI/sample-data only
    KALSHI_REPLAY = "kalshi_replay"  # real recorded Kalshi quotes — the published headline
    STATSBOMB_CALIBRATION = "statsbomb_calibration"  # real xG, NO prices — calibration only
    BETFAIR_EDGE = "betfair_edge"  # real xG + Betfair lines — exchange edge, NOT Kalshi CLV
    ALL = frozenset({SYNTHETIC, KALSHI_REPLAY, STATSBOMB_CALIBRATION, BETFAIR_EDGE})


def require_same_source(*results: "BacktestResult") -> str:
    """Guard against the project's cardinal sin: averaging metrics across data namespaces.
    Raises if the results don't all share one ``data_source``; returns that source.
    Use this anywhere results are combined so conflation fails loudly, not silently."""
    sources = {r.data_source for r in results}
    if len(sources) > 1:
        raise ValueError(
            f"refusing to combine metrics across data sources: {sorted(sources)} — these are "
            "different measurements (real Kalshi / synthetic / StatsBomb / Betfair), not one pool."
        )
    return next(iter(sources)) if sources else "unknown"


@dataclass
class BacktestResult:
    n_matches: int = 0
    n_fills: int = 0
    realized_pnl: float = 0.0
    fees_paid: float = 0.0
    starting_bankroll: float = 0.0
    ending_equity: float = 0.0
    per_match_pnl: list[float] = field(default_factory=list)
    # Same deltas KEYED by match_id. Consumers attributing P&L to a specific match
    # (export_bundles) must use this, never positional alignment: the bare list's
    # order is an artifact of the replay's own id scan, and zipping it against an
    # independently recomputed id list misattributes P&L the moment the DB grew
    # (or the backend returns DISTINCT in a different order) between the two scans.
    pnl_by_match: dict[str, float] = field(default_factory=dict)
    equity_curve: list[float] = field(default_factory=list)
    calibration: dict[str, float] = field(default_factory=dict)
    reliability: list[dict[str, float]] = field(default_factory=list)
    per_outcome_calibration: dict[str, dict[str, float]] = field(default_factory=dict)
    stake_mode: str = "kelly"  # "kelly" (compounding) | "fixed" (constant stake)
    data_source: str = "unknown"  # one of DataSource.* — the namespace this metric lives in
    avg_clv: float = 0.0  # mean closing-line value per contract (probability units)
    clv_n: int = 0  # number of fills with a usable closing price
    # In-play CLV vs the LAST observed tick is degenerate near full time (prices -> 0/1),
    # so for real exchange data we also measure entry vs the PRE-OFF line (the primary,
    # non-degenerate edge signal) and vs the quote ~5 match-minutes later (drift capture).
    avg_clv_preoff: float = 0.0
    clv_n_preoff: int = 0
    avg_clv_5m: float = 0.0
    clv_n_5m: int = 0
    pnl_ci: tuple[float, float] = (0.0, 0.0)  # bootstrap CI for mean per-match P&L
    # MATCH-CLUSTERED 95% CIs for mean CLV (resample matches, re-pool fills). The
    # primary honesty number: the fill-level mean has no valid CI because fills within
    # a match are correlated. ``edge_verdict`` is read off ``clv_ci_preoff``.
    clv_ci_preoff: tuple[float, float] = (0.0, 0.0)
    clv_ci_5m: tuple[float, float] = (0.0, 0.0)
    clv_ci_close: tuple[float, float] = (0.0, 0.0)
    n_clusters_preoff: int = 0  # matches contributing a pre-off fill (the real sample size)
    edge_verdict: str = "indistinguishable_from_zero"

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
            "data_source": self.data_source,
            "realized_pnl": round(self.realized_pnl, 2),
            "fees_paid": round(self.fees_paid, 2),
            "gross_pnl": round(self.gross_pnl, 2),
            "roi": round(self.roi, 4),
            "t_stat": round(self.t_stat, 2),
            "pnl_ci": [round(self.pnl_ci[0], 3), round(self.pnl_ci[1], 3)],
            "avg_clv": round(self.avg_clv, 4),
            "clv_n": self.clv_n,
            "avg_clv_preoff": round(self.avg_clv_preoff, 4),
            "clv_n_preoff": self.clv_n_preoff,
            "avg_clv_5m": round(self.avg_clv_5m, 4),
            "clv_n_5m": self.clv_n_5m,
            "clv_ci_preoff": [round(self.clv_ci_preoff[0], 4), round(self.clv_ci_preoff[1], 4)],
            "clv_ci_5m": [round(self.clv_ci_5m[0], 4), round(self.clv_ci_5m[1], 4)],
            "clv_ci_close": [round(self.clv_ci_close[0], 4), round(self.clv_ci_close[1], 4)],
            "n_clusters_preoff": self.n_clusters_preoff,
            "edge_verdict": self.edge_verdict,
            "starting_bankroll": self.starting_bankroll,
            "ending_equity": round(self.ending_equity, 2),
            "calibration": {k: round(v, 4) for k, v in self.calibration.items()},
            "calibration_per_outcome": {
                o: {k: round(v, 4) for k, v in m.items()}
                for o, m in self.per_outcome_calibration.items()
            },
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
            f"  data source:        {self.data_source}",
            f"  per-match t-stat:   {self.t_stat:.2f}  ({'valid' if self.stake_mode == 'fixed' else 'IGNORE: compounding'})",
            f"  per-match mean 95% CI: [{self.pnl_ci[0]:+.3f}, {self.pnl_ci[1]:+.3f}]  (bootstrap)",
            "-" * 60,
            "  CLV / contract (probability units; positive = we beat the reference)",
            f"  vs pre-off line:    {self.avg_clv_preoff:+.4f}  over {self.clv_n_preoff} fills  (PRIMARY exchange edge)",
            f"  vs +5min line:      {self.avg_clv_5m:+.4f}  over {self.clv_n_5m} fills  (in-play drift)",
            f"  vs last tick:       {self.avg_clv:+.4f}  over {self.clv_n} fills  (WARNING: degenerate near FT)",
            "-" * 60,
            f"  pre-off CLV 95% CI: [{self.clv_ci_preoff[0]:+.4f}, {self.clv_ci_preoff[1]:+.4f}]  "
            f"(MATCH-CLUSTERED, n={self.n_clusters_preoff} matches — the honest sample size)",
            f"  edge verdict:       {self.edge_verdict.replace('_', ' ').upper()}",
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
        # Only a scratch DB WE created gets torn down in aclose (a caller-supplied db is theirs).
        self._temp_db_path: str | None = None
        if db is None:
            fd, path = tempfile.mkstemp(prefix="wck-backtest-", suffix=".sqlite3")
            os.close(fd)
            self._temp_db_path = path
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
        # Per-market mid history (market_ticker -> [(match_minute, mid_prob)]), captured
        # during historical replay so CLV can use a NON-degenerate reference line
        # (pre-off / +5min) instead of only the last tick.
        self._mid_history: dict[str, list[tuple[int, float]]] = {}

    def _collect(self, per_match_pnl: list[float], equity_curve: list[float]) -> BacktestResult:
        rt = self.rt
        if rt.risk.halted:
            # Every match processed after the halt tripped contributed zero fills, so the
            # metrics below describe a truncated sample, not the strategy.
            log.warning(
                "risk halt engaged during backtest — results are censored from the halt point",
                extra={"reason": rt.risk.halt_reason},
            )
        clv = self._compute_clv()
        ci_preoff = clustered_clv_ci(clv["preoff"][2])
        ci_5m = clustered_clv_ci(clv["5m"][2], seed=1)
        ci_close = clustered_clv_ci(clv["close"][2], seed=2)
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
            per_outcome_calibration=rt.calibration.per_outcome_metrics(),
            stake_mode=self.stake_mode,
            avg_clv=clv["close"][0],
            clv_n=clv["close"][1],
            avg_clv_preoff=clv["preoff"][0],
            clv_n_preoff=clv["preoff"][1],
            avg_clv_5m=clv["5m"][0],
            clv_n_5m=clv["5m"][1],
            pnl_ci=bootstrap_ci(per_match_pnl),
            clv_ci_preoff=(ci_preoff[0], ci_preoff[1]),
            clv_ci_5m=(ci_5m[0], ci_5m[1]),
            clv_ci_close=(ci_close[0], ci_close[1]),
            n_clusters_preoff=ci_preoff[2],
            edge_verdict=edge_verdict((ci_preoff[0], ci_preoff[1])),
        )

    @staticmethod
    def _signed_clv(action: str, entry: float, reference: float) -> float:
        """Edge of our entry vs a reference mid, in probability units. Positive = good:
        a buy (long Yes) wins when the reference is higher; a sell when it's lower."""
        return (reference - entry) if action == "buy" else (entry - reference)

    def _reference_mid(self, ticker: str, *, at: int | None, tol: int = 2) -> float | None:
        """Mid for ``ticker`` from the captured tick history. ``at=None`` -> the earliest
        (pre-off) quote; otherwise the quote nearest match-minute ``at`` within ``tol``."""
        hist = self._mid_history.get(ticker)
        if not hist:
            return None
        if at is None:
            # The opening line = the EARLIEST CAPTURED quote. ``hist`` is appended in
            # chronological tick order, so ``hist[0]`` is the pre-off quote. (Do NOT use
            # min-by-minute: a stray late tick stamped minute 0 — a halftime/status reset
            # or post-FT glitch — would otherwise hijack the reference and corrupt CLV.)
            return hist[0][1]
        minute, mid = min(hist, key=lambda mm: abs(mm[0] - at))
        return mid if abs(minute - at) <= tol else None

    def _compute_clv(self) -> dict[str, tuple[float, int, dict[str, list[float]]]]:
        """CLV per contract against three reference lines (probability units).

        * ``close``  — vs the LAST observed mid (``rt.last_mids``). Degenerate near full
          time (prices collapse to 0/1), so it mostly re-states who won; kept for continuity.
        * ``preoff`` — vs the earliest captured quote (the pre-off / opening line). The
          primary, non-degenerate signal of whether the entry beat a real market price.
        * ``5m``     — vs the quote ~5 match-minutes after entry (in-play drift capture).

        Each value is ``(pooled_mean, n_fills, by_match)`` where ``by_match`` maps a
        match_id to its list of per-fill CLVs — the grouping the clustered CI needs.
        """
        rt = self.rt
        buckets: dict[str, list[float]] = {"close": [], "preoff": [], "5m": []}
        by_match: dict[str, dict[str, list[float]]] = {"close": {}, "preoff": {}, "5m": {}}
        for f in rt.fills_log:
            ticker, action = f["market_ticker"], f["action"]
            mid_key = f.get("match_id") or ticker
            entry = f["entry_price_cents"] / 100.0
            close_mid = rt.last_mids.get(ticker)
            if close_mid is not None:
                v = self._signed_clv(action, entry, close_mid)
                buckets["close"].append(v)
                by_match["close"].setdefault(mid_key, []).append(v)
            pre = self._reference_mid(ticker, at=None)
            if pre is not None:
                v = self._signed_clv(action, entry, pre)
                buckets["preoff"].append(v)
                by_match["preoff"].setdefault(mid_key, []).append(v)
            ref5 = self._reference_mid(ticker, at=int(f.get("minute", 0)) + 5)
            if ref5 is not None:
                v = self._signed_clv(action, entry, ref5)
                buckets["5m"].append(v)
                by_match["5m"].setdefault(mid_key, []).append(v)
        return {
            k: ((sum(buckets[k]) / len(buckets[k]) if buckets[k] else 0.0), len(buckets[k]), by_match[k])
            for k in buckets
        }

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
        pnl_by_match: dict[str, float] = {}
        for i in range(n_matches):
            seed = seed0 + i
            snaps = simulate_full_match(
                seed=seed, fixture=FIXTURES[seed % len(FIXTURES)], match_id=f"bt-{seed}"
            )
            n_fills += await self._run_match(snaps)
            delta = rt.portfolio.realized_pnl - prev_realized
            prev_realized = rt.portfolio.realized_pnl
            per_match.append(delta)
            pnl_by_match[f"bt-{seed}"] = delta
            equity_curve.append(rt.portfolio.equity(rt.last_mids))
        result = self._collect(per_match, equity_curve)
        result.n_fills = n_fills
        result.pnl_by_match = pnl_by_match
        result.data_source = DataSource.SYNTHETIC
        return result

    async def run_replay(
        self,
        source_db: Database | None = None,
        *,
        match_ids: list[str] | None = None,
        preloaded: dict[str, tuple[list, list]] | None = None,
    ) -> BacktestResult:
        """Replay stored snapshots from a previous session through the strategy.

        ``preloaded`` maps match_id -> (match_snaps, market_snaps). When given, snapshots come
        from it instead of ``source_db`` — so an export that replays twice AND builds bundles
        can deserialize each match once instead of 3–4×. The per-match processing (bucketing,
        settled skip, P&L) is otherwise identical, so results are unchanged.
        """
        # Full sample for evaluation: the daily-loss halt is a LIVE guardrail keyed to a
        # wall-clock day, but a replay compresses the whole session into one day — left
        # active it silently zeroes every match after the first −$max_daily_loss and
        # censors CLV/verdict (run_synthetic/run_historical already disable it).
        self.rt.risk.limits.max_daily_loss = 1e12
        if preloaded is not None:
            ids = match_ids if match_ids is not None else list(preloaded.keys())
        else:
            assert source_db is not None, "run_replay needs source_db when preloaded is None"
            ids = match_ids or source_db.match_ids()
        per_match: list[float] = []
        pnl_by_match: dict[str, float] = {}
        equity_curve: list[float] = []
        n_fills = 0
        n_skipped = 0
        prev_realized = 0.0
        for match_id in ids:
            if preloaded is not None:
                match_snaps, market_snaps = preloaded[match_id]
            else:
                assert source_db is not None  # narrowed: ids were derived from source_db above
                match_snaps = source_db.iter_match_snapshots(match_id)
                market_snaps = None
            # Only score fully-played matches: one that never reached a settled (FT) state is
            # in-progress or abandoned (e.g. an INTERRUPTED fixture), and its partial fills
            # would pollute CLV/calibration with no real outcome. Skip it.
            if not any(s.period.is_finished for s in match_snaps):
                n_skipped += 1
                log.info("replay: skipping unsettled/abandoned match", extra={"match_id": match_id})
                continue
            if market_snaps is None:  # non-preloaded: load only after the settled check
                assert source_db is not None
                market_snaps = source_db.iter_market_snapshots(match_id)
            ticks = _bucket_market_by_tick(match_snaps, market_snaps)
            st = MatchState(match_id)
            for match, mk in ticks:
                # Capture each market's mid by minute for non-degenerate CLV references
                # (pre-off / +5min), same as run_historical.
                for s in mk:
                    if s.yes_mid_prob is not None:
                        self._mid_history.setdefault(s.market_ticker, []).append(
                            (match.minute, s.yes_mid_prob)
                        )
                summary = await self.processor.process(match, mk, st)  # noqa: F841
            n_fills += st.n_fills
            delta = self.rt.portfolio.realized_pnl - prev_realized
            prev_realized = self.rt.portfolio.realized_pnl
            per_match.append(delta)
            pnl_by_match[match_id] = delta
            equity_curve.append(self.rt.portfolio.equity(self.rt.last_mids))
        result = self._collect(per_match, equity_curve)
        result.n_fills = n_fills
        result.pnl_by_match = pnl_by_match
        result.data_source = DataSource.KALSHI_REPLAY
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
        pnl_by_match: dict[str, float] = {}
        equity_curve: list[float] = []
        n_fills = 0
        prev_realized = 0.0
        for ticks in matches:
            if not ticks:
                continue
            st = MatchState(ticks[0][0].match_id)
            for match, mk in ticks:
                # Capture each market's mid at this minute for non-degenerate CLV refs.
                for s in mk:
                    if s.yes_mid_prob is not None:
                        self._mid_history.setdefault(s.market_ticker, []).append(
                            (match.minute, s.yes_mid_prob)
                        )
                await self.processor.process(match, mk, st)
            n_fills += st.n_fills
            delta = rt.portfolio.realized_pnl - prev_realized
            prev_realized = rt.portfolio.realized_pnl
            per_match.append(delta)
            pnl_by_match[st.match_id] = delta
            equity_curve.append(rt.portfolio.equity(rt.last_mids))
        result = self._collect(per_match, equity_curve)
        result.n_fills = n_fills
        result.pnl_by_match = pnl_by_match
        result.data_source = (
            DataSource.BETFAIR_EDGE if has_market_data(matches) else DataSource.STATSBOMB_CALIBRATION
        )
        return result

    async def _run_match(self, snaps: list[MatchSnapshot]) -> int:
        st = MatchState(snaps[0].match_id)
        for m in snaps:
            mk = await self.rt.market_feed.snapshots_for_match(m)
            await self.processor.process(m, mk, st)
        return st.n_fills

    async def aclose(self) -> None:
        await self.rt.aclose()
        # Tear down the scratch DB we created: dispose the engine pool to release its file
        # handles, then unlink the sqlite file and any WAL/SHM/journal siblings. Otherwise
        # every export run leaks a temp file + a connection pool.
        if self._temp_db_path is not None:
            self.rt.db.engine.dispose()
            for suffix in ("", "-wal", "-shm", "-journal"):
                try:
                    os.unlink(self._temp_db_path + suffix)
                except FileNotFoundError:
                    pass
            self._temp_db_path = None


def _bucket_market_by_tick(
    match_snaps: list[MatchSnapshot], market_snaps: list[MarketSnapshot]
) -> list[tuple[MatchSnapshot, list[MarketSnapshot]]]:
    """Pair each match snapshot with the market snapshots captured at that tick.

    Market snapshots for tick *i* were written just after match snapshot *i*, so they fall in
    ``[ts_i, ts_{i+1})``. Both inputs are ts-ordered, so a single two-pointer merge assigns
    every market snap in O(N+M) — the previous per-tick full scan was O(N×M) (~1.1 s on the
    largest match, ×3 passes per export). market_snaps is sorted defensively (DB rows already
    arrive ts-ordered); a stable sort preserves the original within-bucket order.
    """
    market = sorted(market_snaps, key=lambda s: s.ts)
    out: list[tuple[MatchSnapshot, list[MarketSnapshot]]] = []
    j, m, n = 0, len(market), len(match_snaps)
    for i, match in enumerate(match_snaps):
        lo = match.ts
        hi = match_snaps[i + 1].ts if i + 1 < n else None
        # Skip anything before this bucket (only bites at i=0 / across empty duplicate-ts
        # buckets). `< lo` is strict so a snap AT lo stays for this or a later same-ts bucket.
        while j < m and market[j].ts < lo:
            j += 1
        bucket: list[MarketSnapshot] = []
        while j < m and (hi is None or market[j].ts < hi):
            bucket.append(market[j])
            j += 1
        out.append((match, bucket))
    return out
