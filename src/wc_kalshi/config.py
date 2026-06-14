"""Configuration loading & validation.

Layering (lowest priority first):
    1. config/default.yaml        (checked-in defaults)
    2. config/local.yaml          (git-ignored local overrides, optional)
    3. $WCK_CONFIG file           (optional explicit override file)
    4. environment variables      (mode + ALL secrets)

Secrets are read ONLY from the environment, never from YAML or code.

The `live` run mode is triple-gated: it requires WCK_MODE=live *and*
WCK_ALLOW_LIVE=true *and* execution.live_confirmed=true. Missing any gate while
requesting live is a hard startup error — the system can never *accidentally*
trade real money.
"""

from __future__ import annotations

import enum
import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = REPO_ROOT / "config" / "default.yaml"
LOCAL_CONFIG = REPO_ROOT / "config" / "local.yaml"


class ConfigError(RuntimeError):
    """Raised when configuration is invalid or an unsafe mode is requested."""


class RunMode(str, enum.Enum):
    PAPER = "paper"
    DEMO = "demo"
    LIVE = "live"


# --------------------------------------------------------------------------- #
# Config sections (validated, non-secret).
# --------------------------------------------------------------------------- #
class AppSection(BaseModel):
    log_level: str = "INFO"
    log_format: str = "json"
    data_dir: str = "./data"
    db_url: str = "sqlite:///./data/wck.sqlite3"


class KalshiSection(BaseModel):
    environment: str = "demo"  # demo | prod
    rest_base_demo: str
    ws_base_demo: str
    rest_base_prod: str
    ws_base_prod: str
    worldcup_series_ticker: str = "KXWORLDCUP"
    poll_interval_seconds: float = 2.0
    request_timeout_seconds: float = 10.0
    max_retries: int = 4
    fee_coefficient: float = 0.07
    maker_fee_fraction: float = 0.25


class FootballSection(BaseModel):
    provider: str = "simulated"
    poll_interval_seconds: float = 15.0
    apifootball_base: str = "https://v3.football.api-sports.io"
    thestatsapi_base: str = "https://api.thestatsapi.com"
    request_timeout_seconds: float = 10.0
    max_retries: int = 3
    sim_tick_seconds: float = 1.0
    sim_minutes_per_tick: float = 1.0
    sim_seed: int = 42


class ConsensusSection(BaseModel):
    polymarket_enabled: bool = False
    polymarket_gamma_base: str = "https://gamma-api.polymarket.com"


class ModelSection(BaseModel):
    name: str = "dixon_coles_inplay"
    base_home_xg: float = 1.45
    base_away_xg: float = 1.20
    home_advantage: float = 0.20
    draw_rho: float = -0.05
    live_xg_weight: float = 0.6
    red_card_xg_penalty: float = 0.55
    max_goals: int = 12


class EdgeSection(BaseModel):
    min_edge: float = 0.05
    min_edge_after_costs: float = 0.03
    slippage_cents: int = 1
    devig_method: str = "proportional"


class RiskSection(BaseModel):
    starting_bankroll: float = 1000.0
    kelly_fraction: float = 0.25
    max_position_per_market: int = 100
    max_exposure_per_match: float = 200.0
    max_total_open_exposure: float = 1000.0
    max_daily_loss: float = 250.0
    min_price: float = 0.03
    max_price: float = 0.97
    min_order_contracts: int = 1


class ExecutionSection(BaseModel):
    paper_fill_model: str = "cross_spread"
    live_confirmed: bool = False
    audit_log_path: str = "./data/audit.jsonl"
    order_time_in_force: str = "ioc"
    order_type: str = "limit"
    min_retrade_minutes: int = 8


class DashboardSection(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8000


class AlertsSection(BaseModel):
    enabled: bool = True
    console: bool = True
    webhook: bool = False
    on_goal: bool = True
    on_red_card: bool = True
    on_guardrail: bool = True
    divergence_threshold: float = 0.08


class Secrets(BaseModel):
    """Secret material, sourced ONLY from environment variables."""

    kalshi_api_key_id: str | None = None
    kalshi_private_key_path: str | None = None
    kalshi_private_key_pem: str | None = None
    apifootball_key: str | None = None
    thestatsapi_key: str | None = None
    alerts_webhook_url: str | None = None
    allow_live: bool = False

    def kalshi_private_key(self) -> str | None:
        """Return the RSA private key PEM (file path takes precedence over inline)."""
        if self.kalshi_private_key_path:
            p = Path(self.kalshi_private_key_path).expanduser()
            if not p.exists():
                raise ConfigError(f"KALSHI_PRIVATE_KEY_PATH does not exist: {p}")
            return p.read_text()
        return self.kalshi_private_key_pem

    def has_kalshi_creds(self) -> bool:
        return bool(self.kalshi_api_key_id and self.kalshi_private_key())


class AppConfig(BaseModel):
    mode: RunMode = RunMode.PAPER
    app: AppSection
    kalshi: KalshiSection
    football: FootballSection
    consensus: ConsensusSection
    model: ModelSection
    edge: EdgeSection
    risk: RiskSection
    execution: ExecutionSection
    dashboard: DashboardSection
    alerts: AlertsSection
    secrets: Secrets = Field(default_factory=Secrets)

    # -- convenience accessors ------------------------------------------- #
    @property
    def is_paper(self) -> bool:
        return self.mode is RunMode.PAPER

    @property
    def is_demo(self) -> bool:
        return self.mode is RunMode.DEMO

    @property
    def is_live(self) -> bool:
        return self.mode is RunMode.LIVE

    @property
    def kalshi_rest_base(self) -> str:
        # Demo run mode forces the demo environment regardless of yaml.
        if self.mode is RunMode.LIVE and self.kalshi.environment == "prod":
            return self.kalshi.rest_base_prod
        return self.kalshi.rest_base_demo

    @property
    def kalshi_ws_base(self) -> str:
        if self.mode is RunMode.LIVE and self.kalshi.environment == "prod":
            return self.kalshi.ws_base_prod
        return self.kalshi.ws_base_demo

    def resolved_data_dir(self) -> Path:
        d = (REPO_ROOT / self.app.data_dir).resolve() if not os.path.isabs(
            self.app.data_dir
        ) else Path(self.app.data_dir)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def resolved_db_url(self) -> str:
        """Absolutize a relative sqlite path (relative to repo root) and ensure its
        parent directory exists. Non-sqlite URLs (e.g. Postgres) pass through."""
        prefix = "sqlite:///"
        url = self.app.db_url
        if not url.startswith(prefix):
            return url
        raw = url[len(prefix):]
        path = Path(raw)
        if not path.is_absolute():
            path = (REPO_ROOT / raw).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        return f"{prefix}{path}"

    def resolved_path(self, value: str) -> Path:
        """Resolve a possibly-relative config path against the repo root."""
        p = Path(value).expanduser()
        return p if p.is_absolute() else (REPO_ROOT / p).resolve()


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #
def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in override.items():
        if key in out and isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open() as fh:
        return yaml.safe_load(fh) or {}


def _truthy(value: str | None) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"} if value else False


def _load_secrets() -> Secrets:
    return Secrets(
        kalshi_api_key_id=os.getenv("KALSHI_API_KEY_ID") or None,
        kalshi_private_key_path=os.getenv("KALSHI_PRIVATE_KEY_PATH") or None,
        kalshi_private_key_pem=os.getenv("KALSHI_PRIVATE_KEY_PEM") or None,
        apifootball_key=os.getenv("APIFOOTBALL_KEY") or None,
        thestatsapi_key=os.getenv("THESTATSAPI_KEY") or None,
        alerts_webhook_url=os.getenv("ALERTS_WEBHOOK_URL") or None,
        allow_live=_truthy(os.getenv("WCK_ALLOW_LIVE")),
    )


def load_config(
    config_path: str | os.PathLike[str] | None = None,
    *,
    load_env: bool = True,
) -> AppConfig:
    """Load + validate configuration and resolve the (safe) run mode."""
    if load_env:
        load_dotenv(REPO_ROOT / ".env")

    merged = _load_yaml(DEFAULT_CONFIG)
    merged = _deep_merge(merged, _load_yaml(LOCAL_CONFIG))

    explicit = config_path or os.getenv("WCK_CONFIG")
    if explicit:
        merged = _deep_merge(merged, _load_yaml(Path(explicit)))

    secrets = _load_secrets()

    # Resolve requested mode: env wins over yaml.
    requested = os.getenv("WCK_MODE") or merged.get("mode", "paper")
    try:
        mode = RunMode(str(requested).lower())
    except ValueError as exc:
        raise ConfigError(f"Invalid mode {requested!r}; expected paper|demo|live") from exc

    merged["mode"] = mode.value
    merged["secrets"] = secrets.model_dump()
    config = AppConfig.model_validate(merged)

    _enforce_safety(config)
    return config


def _enforce_safety(config: AppConfig) -> None:
    """Fail loudly rather than ever trade real money by accident."""
    if config.mode is RunMode.LIVE:
        gates = {
            "WCK_ALLOW_LIVE=true": config.secrets.allow_live,
            "execution.live_confirmed=true": config.execution.live_confirmed,
        }
        missing = [name for name, ok in gates.items() if not ok]
        if missing:
            raise ConfigError(
                "Refusing to start in LIVE mode. Live trading requires ALL of: "
                + ", ".join(gates) + ". Missing: " + ", ".join(missing)
                + ". (The system is designed to default to `paper`.)"
            )
    # Demo/live both require Kalshi creds to actually connect; we don't fail here so
    # config still loads for inspection, but the Kalshi client raises clearly when used.
