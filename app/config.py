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


# Public placeholder for TRADINGVIEW_WEBHOOK_SECRET. The boot-time
# validator refuses to start if the live setting equals this value, so
# both the default and the .env.example must reference this single
# constant to keep the check honest.
WEBHOOK_SECRET_PLACEHOLDER = "change_me_to_a_long_random_secret"
WEBHOOK_SECRET_MIN_LENGTH = 16

# Public placeholder for SESSION_SECRET. Same shape as the webhook
# placeholder — validator refuses to start with admin auth on when the
# session secret is this value, empty, or shorter than the minimum.
SESSION_SECRET_PLACEHOLDER = "generate_or_require_secret"
SESSION_SECRET_MIN_LENGTH = 32

# Escape hatch env var. When set to a truthy value the validator emits a
# loud WARNING and lets the app boot anyway. Intended for debug sessions
# only — never set this in production .env.
INSECURE_BOOT_ENV = "SIGNALBRIDGE_ALLOW_INSECURE_BOOT"

# Hosts considered safe to bind without admin auth. Anything else
# requires ADMIN_AUTH_ENABLED=true or the explicit
# SIGNALBRIDGE_ALLOW_PUBLIC_NO_AUTH escape hatch below.
LOCAL_HOSTS: tuple[str, ...] = ("127.0.0.1", "localhost", "::1")

# Separate escape hatch for the M3 bind+auth check. Distinct from
# INSECURE_BOOT_ENV so an operator running behind a trusted reverse
# proxy that handles its own auth can disable just the bind check
# without unmuting the secret-strength gates above. Tailscale Funnel
# does NOT count — it exposes raw HTTP to anyone with the URL.
PUBLIC_NO_AUTH_ENV = "SIGNALBRIDGE_ALLOW_PUBLIC_NO_AUTH"


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

    # Execution state. Three values:
    #   off    — execution disengaged; signals are journaled but no orders
    #            submit. (Risk checks still run, kill switch is irrelevant.)
    #   test   — orders are built and validated against ProjectX schema
    #            but NOT POSTed. Used for smoke-testing plumbing.
    #   armed  — orders submit to ProjectX against the selected Topstep
    #            account. Kill switch / canTrade / armed-symbol allowlist
    #            apply.
    execution_mode: str = Field(
        default_factory=lambda: os.getenv("EXECUTION_MODE", "off").lower()
    )
    # Pinned to "topstep" — the only supported adapter post-collapse.
    # The setting is kept for clarity in /system pages and for any
    # future multi-provider work; coerce() rejects every other value.
    broker_provider: str = Field(
        default_factory=lambda: os.getenv("BROKER_PROVIDER", "topstep").lower()
    )

    webhook_secret: str = Field(
        default_factory=lambda: os.getenv(
            "TRADINGVIEW_WEBHOOK_SECRET", WEBHOOK_SECRET_PLACEHOLDER
        )
    )

    allowed_symbols: List[str] = Field(
        default_factory=lambda: _csv(
            "ALLOWED_SYMBOLS", ["MNQ1!", "MES1!", "NQ1!", "ES1!"]
        )
    )

    # Hard cap on contracts per trade. Applied uniformly across Test
    # and Armed states; the risk engine rejects signals that exceed it
    # before the broker is touched. There is no separate live cap
    # post-collapse — one number, one place.
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

    # Order history defaults used by /api/broker/order-history when the
    # caller does not specify a window. The lookback window is interpreted
    # as days, the limit caps the number of rows returned to the client.
    order_history_lookback_days: int = Field(
        default_factory=lambda: _int("ORDER_HISTORY_LOOKBACK_DAYS", 7)
    )
    order_history_limit: int = Field(
        default_factory=lambda: _int("ORDER_HISTORY_LIMIT", 100)
    )

    # Realtime account/position/order data.
    #
    # ProjectX exposes a SignalR user hub for push updates but the
    # default in this build is polling. Wiring a SignalR client is
    # documented as a future TODO so the same UI works in either mode.
    enable_topstep_realtime: bool = Field(
        default_factory=lambda: _bool("ENABLE_TOPSTEP_REALTIME", False)
    )
    topstep_realtime_mode: str = Field(
        default_factory=lambda: os.getenv("TOPSTEP_REALTIME_MODE", "polling").lower()
    )
    topstep_realtime_poll_seconds: int = Field(
        default_factory=lambda: _int("TOPSTEP_REALTIME_POLL_SECONDS", 5)
    )

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

    # Webhook rate limit (M5). A token-bucket admission filter on
    # /webhooks/tradingview to keep a misconfigured TradingView alert
    # template (firing 100/s in a tight loop) from hammering the broker
    # and saturating the daily-loss limit. Tokens refill at
    # WEBHOOK_RATE_LIMIT_PER_SECOND; the bucket holds up to
    # WEBHOOK_RATE_BURST. Refused requests return 429 and are journaled
    # as ``rate_limited`` rejections so the operator sees them.
    webhook_rate_limit_per_second: float = Field(
        default_factory=lambda: _float("WEBHOOK_RATE_LIMIT_PER_SECOND", 10.0)
    )
    webhook_rate_burst: int = Field(
        default_factory=lambda: _int("WEBHOOK_RATE_BURST", 30)
    )

    # Timezone that defines the trading-day boundary for daily-PnL
    # buckets and "today" counts. Default ``UTC`` matches the storage
    # layout. Operators trading ES/NQ futures typically set this to
    # ``America/New_York`` so the day rollover lines up with the local
    # session instead of 00:00 UTC (= mid-session ET).
    trading_day_timezone: str = Field(
        default_factory=lambda: os.getenv("TRADING_DAY_TIMEZONE", "UTC")
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
    # PBKDF2-SHA256 hash written by /settings/profile. When set, the
    # plaintext ``admin_password`` value is ignored.
    admin_password_hash: str = Field(
        default_factory=lambda: os.getenv("ADMIN_PASSWORD_HASH", "")
    )
    session_secret: str = Field(
        default_factory=lambda: os.getenv(
            "SESSION_SECRET", SESSION_SECRET_PLACEHOLDER
        )
    )

    @property
    def resolved_provider(self) -> str:
        """The active broker provider. Post-collapse this is always
        ``topstep`` — the property is kept so existing callers don't
        need to be rewritten."""
        return (self.broker_provider or "topstep").lower()

    @property
    def resolved_account_id(self) -> str:
        """The selected Topstep account id.

        Prefers SELECTED_ACCOUNT_ID; falls back to TOPSTEP_ACCOUNT_ID.
        Empty string when neither is set — the dashboard surfaces that
        as 'no account selected' and the Armed gate refuses to submit."""
        if self.selected_account_id:
            return self.selected_account_id
        return self.topstep_account_id or ""

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


def validate_secrets(settings: Settings) -> List[str]:
    """Return a list of fatal boot-time problems with the configured secrets.

    Empty list means the configuration is safe to bind. Callers should
    raise ``RuntimeError`` (or the equivalent) when the list is non-empty
    so the process refuses to start.
    """
    errors: List[str] = []

    secret = settings.webhook_secret or ""
    if not secret:
        errors.append(
            "TRADINGVIEW_WEBHOOK_SECRET is unset or empty. "
            "Generate one with: openssl rand -hex 32"
        )
    elif secret == WEBHOOK_SECRET_PLACEHOLDER:
        errors.append(
            "TRADINGVIEW_WEBHOOK_SECRET is still the public placeholder "
            f"({WEBHOOK_SECRET_PLACEHOLDER!r}). "
            "Generate one with: openssl rand -hex 32"
        )
    elif len(secret) < WEBHOOK_SECRET_MIN_LENGTH:
        errors.append(
            "TRADINGVIEW_WEBHOOK_SECRET is shorter than "
            f"{WEBHOOK_SECRET_MIN_LENGTH} characters (got {len(secret)}). "
            "Generate one with: openssl rand -hex 32"
        )

    # M3 — refuse to bind a non-localhost interface with auth disabled.
    # An operator who flips ADMIN_AUTH_ENABLED=false and forgets that
    # APP_HOST is 0.0.0.0 would otherwise expose the dashboard wide
    # open on whatever network the host is on.
    host = (settings.app_host or "").strip()
    if (
        not settings.admin_auth_enabled
        and host
        and host not in LOCAL_HOSTS
        and not _bool(PUBLIC_NO_AUTH_ENV, False)
    ):
        errors.append(
            f"APP_HOST={host!r} binds a non-localhost interface while "
            "ADMIN_AUTH_ENABLED=false. Either enable admin auth or bind "
            f"to one of {LOCAL_HOSTS}. To override for a trusted "
            "reverse-proxy deployment set "
            "SIGNALBRIDGE_ALLOW_PUBLIC_NO_AUTH=1 (Tailscale Funnel does "
            "not count)."
        )

    # SESSION_SECRET is only fatal when admin auth is on — when auth is
    # off, SessionMiddleware isn't installed and the value is unused. A
    # missing secret with auth off still gets a WARNING (see
    # ``enforce_boot_validation``) so an operator who later flips auth on
    # doesn't get a silent forgery surface.
    if settings.admin_auth_enabled:
        session_secret = settings.session_secret or ""
        if not session_secret:
            errors.append(
                "SESSION_SECRET is unset or empty (admin auth is enabled). "
                "Generate one with: openssl rand -hex 32"
            )
        elif session_secret == SESSION_SECRET_PLACEHOLDER:
            errors.append(
                "SESSION_SECRET is still the public placeholder "
                f"({SESSION_SECRET_PLACEHOLDER!r}) (admin auth is enabled). "
                "Generate one with: openssl rand -hex 32"
            )
        elif len(session_secret) < SESSION_SECRET_MIN_LENGTH:
            errors.append(
                "SESSION_SECRET is shorter than "
                f"{SESSION_SECRET_MIN_LENGTH} characters "
                f"(got {len(session_secret)}). "
                "Generate one with: openssl rand -hex 32"
            )

    return errors


def enforce_boot_validation(
    settings: Settings, log: "logging.Logger | None" = None
) -> None:
    """Call ``validate_secrets`` and refuse to boot when it reports problems.

    Honors ``SIGNALBRIDGE_ALLOW_INSECURE_BOOT=1`` as an escape hatch — when
    set, the validator's findings are downgraded to a single ``WARNING``
    log line and boot continues. Intended for debug sessions only.
    """
    import logging as _logging

    if log is None:
        log = _logging.getLogger("signalbridge")

    # Loud WARNING when the public-no-auth escape hatch is on, so the
    # boot logs make it obvious the bind check was deliberately bypassed.
    host = (settings.app_host or "").strip()
    if (
        not settings.admin_auth_enabled
        and host
        and host not in LOCAL_HOSTS
        and _bool(PUBLIC_NO_AUTH_ENV, False)
    ):
        log.warning(
            "SIGNALBRIDGE_ALLOW_PUBLIC_NO_AUTH=1 — binding %s with admin "
            "auth disabled. Only safe behind a trusted reverse proxy "
            "that handles its own authentication.",
            host,
        )

    # Defensive WARNING when admin auth is off but SESSION_SECRET is
    # missing or the placeholder — the operator may flip auth on later
    # and we want them to notice before that's a silent forgery surface.
    if not settings.admin_auth_enabled:
        session_secret = settings.session_secret or ""
        if (
            not session_secret
            or session_secret == SESSION_SECRET_PLACEHOLDER
        ):
            log.warning(
                "SESSION_SECRET is unset or placeholder. Admin auth is "
                "currently disabled so SessionMiddleware is not installed, "
                "but enabling auth without a real secret would leave "
                "sessions forgeable. Generate one with: "
                "openssl rand -hex 32"
            )

    errors = validate_secrets(settings)
    if not errors:
        return

    if _bool(INSECURE_BOOT_ENV, False):
        joined = "; ".join(errors)
        log.warning(
            "SIGNALBRIDGE_ALLOW_INSECURE_BOOT is set — booting despite "
            "fatal secret problems: %s",
            joined,
        )
        return

    bullet = "\n  - "
    message = (
        "SignalBridge refuses to start with insecure configuration:"
        + bullet
        + bullet.join(errors)
        + "\n\nFix each item above and restart. To override for a debug "
        "session set SIGNALBRIDGE_ALLOW_INSECURE_BOOT=1 (loudly logged)."
    )
    raise RuntimeError(message)
