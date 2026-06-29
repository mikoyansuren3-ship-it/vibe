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
import logging
import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field

log = logging.getLogger("wck.config")

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
    worldcup_series_ticker: str = "KXWCGAME"  # 2026 WC match 1X2 (home/away/TIE); verified live
    poll_interval_seconds: float = 2.0
    request_timeout_seconds: float = 10.0
    max_retries: int = 4
    fee_coefficient: float = 0.07
    maker_fee_fraction: float = 0.25
    # Recorder: also capture the broader per-match market set (Total/Spread/BTTS/1H/…) into
    # raw_market_quotes, for researching the roadmap markets. Throttled separately from the
    # match poll since it's many series per fixture. Off by default; on in the recorder.
    capture_extra_markets: bool = False
    extra_markets_interval_seconds: float = 60.0


class FootballSection(BaseModel):
    provider: str = "simulated"
    poll_interval_seconds: float = 15.0
    apifootball_base: str = "https://v3.football.api-sports.io"
    thestatsapi_base: str = "https://api.thestatsapi.com"
    request_timeout_seconds: float = 10.0
    max_retries: int = 3
    # Restrict API-Football live polling to one league (FIFA World Cup = 1) so we
    # only ingest WC fixtures and spend far less of the request quota. None = all live.
    apifootball_league_id: int | None = None
    apifootball_fetch_statistics: bool = True
    apifootball_fetch_context: bool = True  # lineups + injuries (once per match)
    apifootball_fetch_events: bool = True  # goals/cards/subs w/ minute+player (goal timing)
    sim_tick_seconds: float = 1.0
    sim_minutes_per_tick: float = 1.0
    sim_seed: int = 42
    # Shared request budget (token bucket) across all live football polling. The paid
    # API-Football tier allows 75k/day; we never exceed it regardless of match count.
    daily_request_budget: int = 75000
    # Adaptive polling: poll fast when a live match is close & late, slow when idle.
    adaptive_polling: bool = True
    poll_interval_fast_seconds: float = 4.0
    poll_interval_idle_seconds: float = 30.0
    # Simulated-market realism: 0 = market blind to xG (the original strawman),
    # 1 = market fully prices live xG. Used to stress-test that our edge shrinks as
    # the counterparty gets sharper (de-circularises the synthetic backtest).
    sim_market_xg_awareness: float = 0.0


class ModelSection(BaseModel):
    name: str = "dixon_coles_inplay"
    base_home_xg: float = 1.45
    base_away_xg: float = 1.20
    home_advantage: float = 0.20
    draw_rho: float = -0.05
    # Defaults below were fitted on 128 real World Cup matches (see config/default.yaml).
    live_xg_weight: float = 0.3
    red_card_xg_penalty: float = 0.45
    max_goals: int = 12
    # Behavioural constants — previously hard-coded magic numbers, now config-driven
    # so they can be fit against historical data (see modeling/fit.py) and overridden
    # per series without code edits.
    elo_tilt: float = 0.3  # how strongly Elo diff tilts the prior scoring split
    leader_mult: float = 0.92  # remaining-rate multiplier for the team that's ahead
    chaser_mult: float = 1.1  # remaining-rate multiplier for the team that's behind
    # Shot-based xG proxy (modeling/xg_proxy.py) — used when the provider omits live
    # xG (API-Football's in-play WC feed has no expected_goals). Fitted by least
    # squares on 128 StatsBomb WC matches (see scripts/fit_xg_proxy.py).
    xg_proxy_sot: float = 0.1865  # xG per shot on target
    xg_proxy_off: float = 0.0665  # xG per off-target / blocked shot
    xg_proxy_big_chance: float = 0.0  # reserved; WC live feed omits big chances


class EdgeSection(BaseModel):
    min_edge: float = 0.05
    min_edge_after_costs: float = 0.03
    slippage_cents: int = 1
    devig_method: str = "proportional"
    # Market-as-prior shrinkage: blend the model with the de-vigged market via a
    # log-opinion pool, p_eff ∝ p_model**w · p_market**(1-w). 1.0 = pure model (today's
    # behaviour); <1 trusts the (sharper) market more, so we only deviate on real residual
    # signal. Validate w ONLY on held-out data (scripts/eval_market_pool.py) — never tune
    # it on the same matches you then report.
    market_pool_weight: float = 1.0


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
    # Per-position stop: flatten a market when its mark-to-market loss exceeds this
    # fraction of cost paid (0 = disabled). Defence against a position running away
    # mid-match before settlement.
    position_stop_loss: float = 0.0


class ExecutionSection(BaseModel):
    # autonomous = auto-execute actionable edges; advisory = propose & await approval
    decision_mode: str = "autonomous"
    proposal_ttl_seconds: int = 120  # advisory: a pending proposal expires after this
    paper_fill_model: str = "cross_spread"  # cross_spread | midpoint | book (walks depth)
    live_confirmed: bool = False
    audit_log_path: str = "./data/audit.jsonl"
    order_time_in_force: str = "ioc"
    order_type: str = "limit"
    min_retrade_minutes: int = 8
    # Joint 1X2 risk: act on only the single strongest leg per match per tick.
    one_trade_per_match_tick: bool = True
    # Adverse-selection guard: skip if the executable price moved against us by more
    # than this many cents since the signal (0 = disabled; no-op in synthetic backtests
    # where the signal and execution snapshot are the same tick).
    max_adverse_cents: int = 3
    # Late-game exposure taper: shrink size to `late_taper_floor` as minutes_remaining
    # falls below `late_taper_minutes` (0 minutes = disabled).
    late_taper_minutes: int = 15
    late_taper_floor: float = 0.34
    # Kill switch flattens open inventory by placing closing orders.
    flatten_on_kill: bool = True
    # Cancel resting (unfilled) orders after this many seconds (live order lifecycle).
    resting_timeout_seconds: float = 30.0


class DashboardSection(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8000


class AlertsSection(BaseModel):
    enabled: bool = True
    console: bool = True
    webhook: bool = False
    discord: bool = False
    telegram: bool = False
    email: bool = False
    on_goal: bool = True
    on_red_card: bool = True
    on_guardrail: bool = True
    on_proposal: bool = True
    on_fill: bool = True
    divergence_threshold: float = 0.08


class Secrets(BaseModel):
    """Secret material, sourced ONLY from environment variables."""

    kalshi_api_key_id: str | None = None
    kalshi_private_key_path: str | None = None
    kalshi_private_key_pem: str | None = None
    apifootball_key: str | None = None
    thestatsapi_key: str | None = None
    alerts_webhook_url: str | None = None
    discord_webhook_url: str | None = None
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    smtp_host: str | None = None
    smtp_port: int | None = None
    smtp_user: str | None = None
    smtp_password: str | None = None
    email_from: str | None = None
    email_to: str | None = None
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
        discord_webhook_url=os.getenv("DISCORD_WEBHOOK_URL") or None,
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN") or None,
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID") or None,
        smtp_host=os.getenv("SMTP_HOST") or None,
        smtp_port=int(os.getenv("SMTP_PORT")) if os.getenv("SMTP_PORT") else None,
        smtp_user=os.getenv("SMTP_USER") or None,
        smtp_password=os.getenv("SMTP_PASSWORD") or None,
        email_from=os.getenv("EMAIL_FROM") or None,
        email_to=os.getenv("EMAIL_TO") or None,
        allow_live=_truthy(os.getenv("WCK_ALLOW_LIVE")),
    )


def load_config(
    config_path: str | os.PathLike[str] | None = None,
    *,
    load_env: bool = True,
    use_local: bool = True,
) -> AppConfig:
    """Load + validate configuration and resolve the (safe) run mode.

    ``use_local=False`` skips the developer's git-ignored ``config/local.yaml`` so tests
    are deterministic regardless of local overrides (fees, providers, capture flags, …).
    """
    if load_env:
        load_dotenv(REPO_ROOT / ".env")

    merged = _load_yaml(DEFAULT_CONFIG)
    layers = ["config/default.yaml"]
    local_missing = False
    if use_local:
        if LOCAL_CONFIG.exists():
            merged = _deep_merge(merged, _load_yaml(LOCAL_CONFIG))
            layers.append("config/local.yaml")
        else:
            local_missing = True

    explicit = config_path or os.getenv("WCK_CONFIG")
    if explicit:
        merged = _deep_merge(merged, _load_yaml(Path(explicit)))
        layers.append(str(explicit))

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
    _log_provenance(config, layers, local_missing=local_missing)
    return config


def _log_provenance(config: AppConfig, layers: list[str], *, local_missing: bool) -> None:
    """Make the config that's actually in effect visible every run — never silent.

    The operationally-critical knobs (``fee_coefficient``, ``capture_extra_markets``) live
    only in git-ignored ``config/local.yaml``, so a real-data run without it silently
    reverts to a phantom 0.07 fee + 1X2-only capture. Log the resolved values, loudly."""
    log.info(
        "config: layers=%s mode=%s provider=%s fee=%s capture_extra=%s",
        "+".join(layers), config.mode.value, config.football.provider,
        config.kalshi.fee_coefficient, config.kalshi.capture_extra_markets,
    )
    if local_missing and config.football.provider == "apifootball":
        log.warning(
            "config/local.yaml MISSING but provider=apifootball — fee_coefficient=%s and "
            "capture_extra_markets=%s fell back to defaults; the recorder overrides are NOT "
            "applied. Create config/local.yaml (or pass --config) before recording.",
            config.kalshi.fee_coefficient, config.kalshi.capture_extra_markets,
        )


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
