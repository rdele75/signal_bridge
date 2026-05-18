"""Application configuration loaded from environment / .env."""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import List

from dotenv import load_dotenv
from pydantic import BaseModel, Field


PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Load .env if present. Safe to call repeatedly.
load_dotenv(PROJECT_ROOT / ".env")


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _csv(name: str, default: List[str]) -> List[str]:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return list(default)
    return [item.strip() for item in raw.split(",") if item.strip()]


class Settings(BaseModel):
    app_name: str = Field(default_factory=lambda: os.getenv("APP_NAME", "SignalBridge"))
    app_host: str = Field(default_factory=lambda: os.getenv("APP_HOST", "127.0.0.1"))
    app_port: int = Field(default_factory=lambda: _int("APP_PORT", 8000))

    execution_mode: str = Field(
        default_factory=lambda: os.getenv("EXECUTION_MODE", "paper").lower()
    )
    # `broker_provider` is the canonical selector. `broker` is kept for
    # backwards compatibility — if `broker_provider` is unset, signal_router
    # falls back to `broker`. Both default to "paper".
    broker_provider: str = Field(
        default_factory=lambda: os.getenv("BROKER_PROVIDER", "").lower()
    )
    broker: str = Field(default_factory=lambda: os.getenv("BROKER", "paper").lower())

    webhook_secret: str = Field(
        default_factory=lambda: os.getenv(
            "TRADINGVIEW_WEBHOOK_SECRET", "change_me_to_a_long_random_secret"
        )
    )

    allowed_symbols: List[str] = Field(
        default_factory=lambda: _csv("ALLOWED_SYMBOLS", ["MES1!", "MNQ1!"])
    )

    max_contracts_per_trade: int = Field(
        default_factory=lambda: _int("MAX_CONTRACTS_PER_TRADE", 1)
    )
    # When true (the default), trade sizing is taken from the TradingView
    # alert's ``contracts`` field — the strategy owns position size.
    # When false, the alert's contracts are ignored for execution sizing
    # and ``fixed_contracts_per_trade`` is used instead. The hard cap
    # ``max_contracts_per_trade`` is always enforced on top.
    strategy_managed_risk: bool = Field(
        default_factory=lambda: _bool("STRATEGY_MANAGED_RISK", True)
    )
    fixed_contracts_per_trade: int = Field(
        default_factory=lambda: _int("FIXED_CONTRACTS_PER_TRADE", 1)
    )
    max_daily_loss: float = Field(default_factory=lambda: _float("MAX_DAILY_LOSS", 250.0))
    max_open_positions: int = Field(
        default_factory=lambda: _int("MAX_OPEN_POSITIONS", 1)
    )

    enable_longs: bool = Field(default_factory=lambda: _bool("ENABLE_LONGS", True))
    enable_shorts: bool = Field(default_factory=lambda: _bool("ENABLE_SHORTS", True))
    enable_kill_switch: bool = Field(
        default_factory=lambda: _bool("ENABLE_KILL_SWITCH", True)
    )

    database_path: str = Field(
        default_factory=lambda: os.getenv("DATABASE_PATH", "data/signalbridge.db")
    )
    log_path: str = Field(
        default_factory=lambda: os.getenv("LOG_PATH", "logs/signalbridge.log")
    )
    log_level: str = Field(
        default_factory=lambda: os.getenv("LOG_LEVEL", "INFO").upper()
    )

    # Topstep / TopstepX — primary planned futures broker via ProjectX API.
    # The adapter is scaffolded-only right now; real auth/order routing is
    # not implemented. These values are accepted, persisted, and surfaced
    # through the dashboard (masked) so the wiring is ready for the future
    # authentication phase.
    topstep_username: str = Field(default_factory=lambda: os.getenv("TOPSTEP_USERNAME", ""))
    topstep_password: str = Field(default_factory=lambda: os.getenv("TOPSTEP_PASSWORD", ""))
    topstep_api_key: str = Field(default_factory=lambda: os.getenv("TOPSTEP_API_KEY", ""))
    topstep_account_id: str = Field(
        default_factory=lambda: os.getenv("TOPSTEP_ACCOUNT_ID", "")
    )
    topstep_env: str = Field(default_factory=lambda: os.getenv("TOPSTEP_ENV", "demo"))
    topstep_base_url: str = Field(
        default_factory=lambda: os.getenv("TOPSTEP_BASE_URL", "https://api.topstepx.com")
    )
    topstep_ws_url: str = Field(
        default_factory=lambda: os.getenv("TOPSTEP_WS_URL", "https://rtc.topstepx.com")
    )
    # Cached auth artifacts. Written by the adapter once real auth lands;
    # empty in the scaffolded build.
    topstep_token: str = Field(default_factory=lambda: os.getenv("TOPSTEP_TOKEN", ""))
    topstep_token_expires_at: str = Field(
        default_factory=lambda: os.getenv("TOPSTEP_TOKEN_EXPIRES_AT", "")
    )

    # Topstep order routing safety switches. By default the adapter only
    # builds dry-run order previews — nothing reaches /api/Order/place.
    # To enable demo/sim execution the operator must also set
    # TOPSTEP_EXECUTION_CONFIRM=DEMO_ONLY, EXECUTION_MODE=demo, and
    # BROKER_PROVIDER=topstep. Live/funded execution stays blocked.
    enable_topstep_order_dry_run: bool = Field(
        default_factory=lambda: _bool("ENABLE_TOPSTEP_ORDER_DRY_RUN", True)
    )
    enable_topstep_order_execution: bool = Field(
        default_factory=lambda: _bool("ENABLE_TOPSTEP_ORDER_EXECUTION", False)
    )
    topstep_execution_confirm: str = Field(
        default_factory=lambda: os.getenv(
            "TOPSTEP_EXECUTION_CONFIRM", "disabled"
        )
    )
    # Hard kill: even if every other flag is on, this MUST be false for any
    # order to leave the building. Lives here so a future "unlock live"
    # phase has one obvious place to flip — and tests can assert that a
    # true value blocks demo execution too (defense in depth).
    enable_live_trading: bool = Field(
        default_factory=lambda: _bool("ENABLE_LIVE_TRADING", False)
    )

    # Tradovate placeholders — not used until the adapter is implemented.
    tradovate_username: str = Field(
        default_factory=lambda: os.getenv("TRADOVATE_USERNAME", "")
    )
    tradovate_password: str = Field(
        default_factory=lambda: os.getenv("TRADOVATE_PASSWORD", "")
    )
    tradovate_app_id: str = Field(
        default_factory=lambda: os.getenv("TRADOVATE_APP_ID", "")
    )
    tradovate_app_version: str = Field(
        default_factory=lambda: os.getenv("TRADOVATE_APP_VERSION", "")
    )
    tradovate_cid: str = Field(default_factory=lambda: os.getenv("TRADOVATE_CID", ""))
    tradovate_sec: str = Field(default_factory=lambda: os.getenv("TRADOVATE_SEC", ""))
    tradovate_account_id: str = Field(
        default_factory=lambda: os.getenv("TRADOVATE_ACCOUNT_ID", "")
    )
    tradovate_env: str = Field(default_factory=lambda: os.getenv("TRADOVATE_ENV", "demo"))

    # Currently selected trading account. Persisted alongside the broker
    # provider. Empty string means "use the per-provider default".
    selected_account_id: str = Field(
        default_factory=lambda: os.getenv("SELECTED_ACCOUNT_ID", "")
    )

    # Optional symbol-mapping file (provider-aware mappings). Resolved
    # relative to the project root if not absolute.
    symbols_map_path: str = Field(
        default_factory=lambda: os.getenv("SYMBOLS_MAP_PATH", "config/symbols.json")
    )

    # Duplicate order_id rejection window (seconds).
    duplicate_order_cooldown_seconds: int = Field(
        default_factory=lambda: _int("DUPLICATE_ORDER_COOLDOWN_SECONDS", 60)
    )

    # Reject signals whose chart timeframe isn't in the allowlist.
    enable_timeframe_lock: bool = Field(
        default_factory=lambda: _bool("ENABLE_TIMEFRAME_LOCK", False)
    )
    allowed_timeframes: List[str] = Field(
        default_factory=lambda: _csv("ALLOWED_TIMEFRAMES", ["1"])
    )

    # Dashboard admin authentication. Required before exposing the
    # dashboard publicly (e.g. via Tailscale Funnel). The webhook
    # endpoint stays public — it has its own shared-secret check.
    admin_auth_enabled: bool = Field(
        default_factory=lambda: _bool("ADMIN_AUTH_ENABLED", True)
    )
    admin_username: str = Field(
        default_factory=lambda: os.getenv("ADMIN_USERNAME", "admin")
    )
    admin_password: str = Field(
        default_factory=lambda: os.getenv(
            "ADMIN_PASSWORD", "change_me_admin_password"
        )
    )
    session_secret: str = Field(
        default_factory=lambda: os.getenv(
            "SESSION_SECRET", "generate_or_require_secret"
        )
    )

    @property
    def resolved_provider(self) -> str:
        """The provider to actually use. Prefers BROKER_PROVIDER, falls back
        to BROKER (legacy), defaults to 'paper'."""
        return (self.broker_provider or self.broker or "paper").lower()

    @property
    def resolved_account_id(self) -> str:
        """The selected account id for the active provider.

        Prefers SELECTED_ACCOUNT_ID. Otherwise falls back to the
        per-provider account id (TOPSTEP_ACCOUNT_ID / TRADOVATE_ACCOUNT_ID)
        or the paper default 'PAPER-001'.
        """
        if self.selected_account_id:
            return self.selected_account_id
        provider = self.resolved_provider
        if provider == "topstep":
            return self.topstep_account_id or ""
        if provider == "tradovate":
            return self.tradovate_account_id or ""
        return "PAPER-001"

    @property
    def symbols_map_abs_path(self) -> Path:
        p = Path(self.symbols_map_path)
        return p if p.is_absolute() else PROJECT_ROOT / p

    @property
    def database_abs_path(self) -> Path:
        p = Path(self.database_path)
        return p if p.is_absolute() else PROJECT_ROOT / p

    @property
    def log_abs_path(self) -> Path:
        p = Path(self.log_path)
        return p if p.is_absolute() else PROJECT_ROOT / p

    def ensure_dirs(self) -> None:
        self.database_abs_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_abs_path.parent.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    s = Settings()
    s.ensure_dirs()
    return s


def reload_settings() -> Settings:
    """Force re-read of environment. Used in tests."""
    get_settings.cache_clear()
    return get_settings()
