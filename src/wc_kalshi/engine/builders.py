"""Wire all pipeline components together from a single config.

Used by both the live orchestrator and the backtest harness, so they exercise the
*same* model/edge/sizing/risk/execution objects — what you backtest is what you run.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..config import AppConfig
from ..edge.detector import EdgeDetector
from ..eventbus import Event, EventBus, EventType
from ..execution.audit import AuditLogger
from ..execution.base import Executor
from ..execution.paper import PaperExecutor
from ..execution.portfolio import Portfolio
from ..ingestion.kalshi.feed import MarketFeed, SimulatedMarketFeed
from ..logging_setup import get_logger
from ..modeling.base import ProbabilityModel, build_model
from ..modeling.calibration import CalibrationTracker
from ..models.db import Database
from ..risk.guardrails import RiskLimits, RiskManager
from ..risk.sizing import PositionSizer
from .state import RuntimeState

log = get_logger("engine.builder")


@dataclass
class Runtime:
    cfg: AppConfig
    db: Database
    bus: EventBus
    model: ProbabilityModel
    detector: EdgeDetector
    sizer: PositionSizer
    risk: RiskManager
    portfolio: Portfolio
    audit: AuditLogger
    calibration: CalibrationTracker
    market_feed: MarketFeed
    executor: Executor
    state: RuntimeState
    kalshi_client: Any | None = None
    # Latest Yes mid (probability) per market ticker, ACROSS all matches, so the
    # whole open book is marked to market (not just the match being processed).
    last_mids: dict[str, float] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.last_mids is None:
            self.last_mids = {}

    async def aclose(self) -> None:
        for closer in (self.market_feed, self.executor):
            try:
                await closer.aclose()
            except Exception:
                pass
        if self.kalshi_client is not None:
            try:
                await self.kalshi_client.aclose()
            except Exception:
                pass


def _build_kalshi_client(cfg: AppConfig):
    from ..ingestion.kalshi.auth import KalshiSigner
    from ..ingestion.kalshi.client import KalshiClient

    if not cfg.secrets.has_kalshi_creds():
        raise ValueError(
            f"mode={cfg.mode.value} needs Kalshi credentials "
            "(KALSHI_API_KEY_ID + KALSHI_PRIVATE_KEY_PATH/PEM)."
        )
    signer = KalshiSigner(cfg.secrets.kalshi_api_key_id, cfg.secrets.kalshi_private_key())
    return KalshiClient(
        cfg.kalshi_rest_base,
        signer=signer,
        timeout=cfg.kalshi.request_timeout_seconds,
        max_retries=cfg.kalshi.max_retries,
    )


def build_runtime(cfg: AppConfig, *, db: Database | None = None, bus: EventBus | None = None) -> Runtime:
    db = db or Database(cfg.resolved_db_url())
    bus = bus or EventBus()

    model = build_model(cfg)
    detector = EdgeDetector.from_config(cfg)
    sizer = PositionSizer.from_config(cfg)
    risk = RiskManager(limits=RiskLimits.from_config(cfg))
    portfolio = Portfolio(starting_bankroll=cfg.risk.starting_bankroll)
    audit = AuditLogger(cfg.resolved_path(cfg.execution.audit_log_path), db=db)
    calibration = CalibrationTracker()

    kalshi_client = None
    market_feed: MarketFeed
    executor: Executor

    if cfg.is_paper:
        market_feed = SimulatedMarketFeed(seed=cfg.football.sim_seed)
        executor = PaperExecutor(
            fill_model=cfg.execution.paper_fill_model,
            fee_coefficient=cfg.kalshi.fee_coefficient,
            maker_fraction=cfg.kalshi.maker_fee_fraction,
        )
    else:
        from ..execution.kalshi_exec import KalshiExecutor
        from ..ingestion.kalshi.feed import LiveKalshiMarketFeed

        kalshi_client = _build_kalshi_client(cfg)
        market_feed = LiveKalshiMarketFeed(
            kalshi_client, series_ticker=cfg.kalshi.worldcup_series_ticker
        )
        executor = KalshiExecutor(
            kalshi_client,
            mode=cfg.mode.value,
            order_type=cfg.execution.order_type,
            fee_coefficient=cfg.kalshi.fee_coefficient,
        )

    state = RuntimeState(mode=cfg.mode.value)

    # Wire guardrail trips to the audit log + event bus (for alerts/dashboard).
    def _on_halt(reason: str) -> None:
        audit.guardrail(reason)
        bus.publish(Event(EventType.GUARDRAIL, {"reason": reason}))
        log.error("GUARDRAIL TRIPPED", extra={"reason": reason})

    risk.on_halt = _on_halt

    log.info(
        "runtime built",
        extra={
            "mode": cfg.mode.value,
            "model": model.name,
            "executor": executor.mode,
            "feed": type(market_feed).__name__,
        },
    )
    return Runtime(
        cfg=cfg,
        db=db,
        bus=bus,
        model=model,
        detector=detector,
        sizer=sizer,
        risk=risk,
        portfolio=portfolio,
        audit=audit,
        calibration=calibration,
        market_feed=market_feed,
        executor=executor,
        state=state,
        kalshi_client=kalshi_client,
    )
