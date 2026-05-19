"""SQLite-backed settings store.

`.env` provides defaults at first boot. Anything the user changes through
the dashboard is persisted in the `settings` table of the same SQLite
database that holds the journal, and overrides the `.env` value at
runtime. Broker credentials are intentionally NOT stored through the UI
yet — see the README.
"""
from __future__ import annotations

import secrets
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .config import Settings


# Keys the dashboard is allowed to read/write. Everything else stays
# env-only.
MANAGED_KEYS: tuple[str, ...] = (
    "APP_HOST",
    "APP_PORT",
    "EXECUTION_MODE",
    "BROKER_PROVIDER",
    "SELECTED_ACCOUNT_ID",
    "TRADINGVIEW_WEBHOOK_SECRET",
    "ALLOWED_SYMBOLS",
    "MAX_CONTRACTS_PER_TRADE",
    "STRATEGY_MANAGED_RISK",
    "FIXED_CONTRACTS_PER_TRADE",
    "MAX_DAILY_LOSS",
    "MAX_OPEN_POSITIONS",
    "ENABLE_LONGS",
    "ENABLE_SHORTS",
    "DUPLICATE_ORDER_COOLDOWN_SECONDS",
    "ENABLE_TIMEFRAME_LOCK",
    "ALLOWED_TIMEFRAMES",
    # Topstep / TopstepX (ProjectX) — credentials + cached auth artifacts.
    # TOKEN / TOKEN_EXPIRES_AT are written by the adapter after a successful
    # loginKey call, never by the user, but they need to be persisted so the
    # token survives a restart.
    "TOPSTEP_USERNAME",
    "TOPSTEP_API_KEY",
    "TOPSTEP_ACCOUNT_ID",
    "TOPSTEP_ENV",
    "TOPSTEP_BASE_URL",
    "TOPSTEP_WS_URL",
    "TOPSTEP_TOKEN",
    "TOPSTEP_TOKEN_EXPIRES_AT",
    # Order routing safety switches. False by default. Live/funded
    # execution stays blocked across all combinations.
    "ENABLE_TOPSTEP_ORDER_DRY_RUN",
    "ENABLE_TOPSTEP_ORDER_EXECUTION",
    "TOPSTEP_EXECUTION_CONFIRM",
    "ENABLE_LIVE_TRADING",
    # Dashboard admin credentials. ADMIN_PASSWORD_HASH is written by the
    # Profile page; the plaintext ADMIN_PASSWORD env var stays a fallback
    # for first-run installs that haven't visited the Profile page yet.
    "ADMIN_USERNAME",
    "ADMIN_PASSWORD_HASH",
)

# Keys whose change can be applied to the in-memory Settings instance
# without a restart. Provider switches and bind-address changes do not
# take effect until the app restarts, because the broker adapter and
# uvicorn binding are constructed once at startup.
RUNTIME_APPLICABLE: frozenset[str] = frozenset(
    {
        "EXECUTION_MODE",
        "SELECTED_ACCOUNT_ID",
        "TRADINGVIEW_WEBHOOK_SECRET",
        "ALLOWED_SYMBOLS",
        "MAX_CONTRACTS_PER_TRADE",
        "STRATEGY_MANAGED_RISK",
        "FIXED_CONTRACTS_PER_TRADE",
        "MAX_DAILY_LOSS",
        "MAX_OPEN_POSITIONS",
        "ENABLE_LONGS",
        "ENABLE_SHORTS",
        "DUPLICATE_ORDER_COOLDOWN_SECONDS",
        "ENABLE_TIMEFRAME_LOCK",
        "ALLOWED_TIMEFRAMES",
        # Topstep credentials are read by the adapter on each call, so
        # changes don't need a restart to take effect on the next test
        # connection / API call. The active broker instance still needs
        # a restart if BROKER_PROVIDER changes.
        "TOPSTEP_USERNAME",
        "TOPSTEP_API_KEY",
        "TOPSTEP_ACCOUNT_ID",
        "TOPSTEP_ENV",
        "TOPSTEP_BASE_URL",
        "TOPSTEP_WS_URL",
        "TOPSTEP_TOKEN",
        "TOPSTEP_TOKEN_EXPIRES_AT",
        "ENABLE_TOPSTEP_ORDER_DRY_RUN",
        "ENABLE_TOPSTEP_ORDER_EXECUTION",
        "TOPSTEP_EXECUTION_CONFIRM",
        "ENABLE_LIVE_TRADING",
        # Auth settings take effect on the next login attempt — no
        # restart needed because check_credentials reads them per-call.
        "ADMIN_USERNAME",
        "ADMIN_PASSWORD_HASH",
    }
)

# Keys that require a restart to fully take effect.
RESTART_REQUIRED: frozenset[str] = frozenset(
    {"APP_HOST", "APP_PORT", "BROKER_PROVIDER"}
)

_TRUE_STRINGS = {"1", "true", "yes", "on"}
_FALSE_STRINGS = {"0", "false", "no", "off"}

ALLOWED_PROVIDERS: tuple[str, ...] = ("paper", "topstep", "tradovate")
ALLOWED_EXECUTION_MODES: tuple[str, ...] = ("paper", "demo", "live")
ALLOWED_TOPSTEP_ENVS: tuple[str, ...] = ("demo", "live")


class SettingsValidationError(ValueError):
    """Raised when an incoming setting value can't be coerced or violates
    a domain rule (e.g. EXECUTION_MODE=live)."""


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------
# Parsers / serializers
# ---------------------------------------------------------------------

def parse_bool(raw: Any) -> bool:
    if isinstance(raw, bool):
        return raw
    if raw is None:
        raise SettingsValidationError("expected a boolean, got null")
    text = str(raw).strip().lower()
    if text in _TRUE_STRINGS:
        return True
    if text in _FALSE_STRINGS:
        return False
    raise SettingsValidationError(f"not a boolean: {raw!r}")


def parse_int(raw: Any, *, min_value: Optional[int] = None) -> int:
    if raw is None or raw == "":
        raise SettingsValidationError("expected an integer, got empty value")
    try:
        value = int(float(raw))
    except (TypeError, ValueError) as exc:
        raise SettingsValidationError(f"not an integer: {raw!r}") from exc
    if min_value is not None and value < min_value:
        raise SettingsValidationError(f"must be >= {min_value} (got {value})")
    return value


def parse_float(raw: Any, *, min_value: Optional[float] = None) -> float:
    if raw is None or raw == "":
        raise SettingsValidationError("expected a number, got empty value")
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise SettingsValidationError(f"not a number: {raw!r}") from exc
    if min_value is not None and value < min_value:
        raise SettingsValidationError(f"must be >= {min_value} (got {value})")
    return value


def parse_symbols(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        items = [str(s).strip() for s in raw]
    else:
        items = [item.strip() for item in str(raw).split(",")]
    items = [s for s in items if s]
    return items


def serialize_symbols(symbols: list[str]) -> str:
    return ",".join(s.strip() for s in symbols if s and str(s).strip())


def parse_timeframes(raw: Any) -> list[str]:
    """Parse a user-supplied list of timeframes ("1,5,15" / [1, "5"]) into
    normalized string entries. Invalid entries are dropped silently — the
    risk engine treats an empty allow-list as 'lock has nothing to match'
    and rejects, which is the safe direction. An empty input yields []."""
    from .risk_engine import normalize_timeframe  # local: avoid cycle

    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        items = list(raw)
    else:
        items = [item.strip() for item in str(raw).split(",")]
    out: list[str] = []
    for item in items:
        normalized = normalize_timeframe(item)
        if normalized is None or normalized == "":
            continue
        if normalized in out:
            continue
        out.append(normalized)
    return out


def serialize(key: str, value: Any) -> str:
    """Turn a typed Settings value into the string we store in SQLite."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return serialize_symbols(value)
    return str(value)


def coerce(key: str, raw: Any) -> Any:
    """Validate + coerce an incoming string value for `key` to its proper
    Python type. Raises SettingsValidationError on bad input."""
    if key == "APP_HOST":
        text = (str(raw) if raw is not None else "").strip()
        if not text:
            raise SettingsValidationError("APP_HOST cannot be empty")
        return text

    if key == "APP_PORT":
        port = parse_int(raw, min_value=1)
        if port > 65535:
            raise SettingsValidationError("APP_PORT must be <= 65535")
        return port

    if key == "EXECUTION_MODE":
        text = str(raw or "").strip().lower()
        if text not in ALLOWED_EXECUTION_MODES:
            raise SettingsValidationError(
                f"EXECUTION_MODE must be one of {ALLOWED_EXECUTION_MODES}"
            )
        if text == "live":
            raise SettingsValidationError(
                "live execution is not allowed yet — pick paper or demo"
            )
        return text

    if key == "BROKER_PROVIDER":
        text = str(raw or "").strip().lower()
        if text not in ALLOWED_PROVIDERS:
            raise SettingsValidationError(
                f"BROKER_PROVIDER must be one of {ALLOWED_PROVIDERS}"
            )
        return text

    if key == "SELECTED_ACCOUNT_ID":
        text = (str(raw) if raw is not None else "").strip()
        # Empty is allowed — means "use the per-provider default".
        if len(text) > 128:
            raise SettingsValidationError(
                "SELECTED_ACCOUNT_ID must be 128 characters or fewer"
            )
        return text

    if key == "TRADINGVIEW_WEBHOOK_SECRET":
        text = str(raw or "")
        if len(text) < 8:
            raise SettingsValidationError(
                "TRADINGVIEW_WEBHOOK_SECRET must be at least 8 characters"
            )
        return text

    if key == "ALLOWED_SYMBOLS":
        return parse_symbols(raw)

    if key in {
        "MAX_CONTRACTS_PER_TRADE",
        "MAX_OPEN_POSITIONS",
        "FIXED_CONTRACTS_PER_TRADE",
    }:
        return parse_int(raw, min_value=1)

    if key == "STRATEGY_MANAGED_RISK":
        return parse_bool(raw)

    if key == "MAX_DAILY_LOSS":
        return parse_float(raw, min_value=0.0)

    if key == "DUPLICATE_ORDER_COOLDOWN_SECONDS":
        return parse_int(raw, min_value=0)

    if key in {"ENABLE_LONGS", "ENABLE_SHORTS", "ENABLE_TIMEFRAME_LOCK"}:
        return parse_bool(raw)

    if key == "ALLOWED_TIMEFRAMES":
        return parse_timeframes(raw)

    # ---- Topstep / TopstepX (ProjectX) ----
    if key in {"TOPSTEP_USERNAME", "TOPSTEP_ACCOUNT_ID"}:
        text = (str(raw) if raw is not None else "").strip()
        if len(text) > 256:
            raise SettingsValidationError(f"{key} must be 256 characters or fewer")
        return text

    if key == "TOPSTEP_API_KEY":
        # Allow empty (cleared) or any non-empty secret string. We do not
        # impose a strict format because ProjectX issues opaque keys.
        # Stripped because dashboard paste-in commonly carries a trailing
        # newline / space, which causes silent errorCode=3 on ProjectX.
        text = (str(raw) if raw is not None else "").strip()
        if len(text) > 1024:
            raise SettingsValidationError("TOPSTEP_API_KEY is unexpectedly long")
        return text

    if key == "TOPSTEP_ENV":
        text = (str(raw) if raw is not None else "").strip().lower() or "demo"
        if text not in ALLOWED_TOPSTEP_ENVS:
            raise SettingsValidationError(
                f"TOPSTEP_ENV must be one of {ALLOWED_TOPSTEP_ENVS}"
            )
        if text == "live":
            raise SettingsValidationError(
                "TOPSTEP_ENV=live is not allowed yet — pick demo"
            )
        return text

    if key in {"TOPSTEP_BASE_URL", "TOPSTEP_WS_URL"}:
        text = (str(raw) if raw is not None else "").strip()
        if text and not (text.startswith("http://") or text.startswith("https://")):
            raise SettingsValidationError(
                f"{key} must be a http(s) URL"
            )
        if len(text) > 512:
            raise SettingsValidationError(f"{key} is too long")
        return text

    if key == "TOPSTEP_TOKEN":
        # Opaque JWT-ish blob written by the adapter. Allow empty (cleared)
        # or any non-empty value up to a generous ceiling.
        text = str(raw) if raw is not None else ""
        if len(text) > 8192:
            raise SettingsValidationError("TOPSTEP_TOKEN is unexpectedly long")
        return text

    if key == "TOPSTEP_TOKEN_EXPIRES_AT":
        text = (str(raw) if raw is not None else "").strip()
        if len(text) > 64:
            raise SettingsValidationError("TOPSTEP_TOKEN_EXPIRES_AT is too long")
        return text

    if key in {"ENABLE_TOPSTEP_ORDER_DRY_RUN", "ENABLE_TOPSTEP_ORDER_EXECUTION"}:
        return parse_bool(raw)

    if key == "ENABLE_LIVE_TRADING":
        # Live/funded execution is intentionally locked in this build. The
        # only honored value is False. A True submission is refused so the
        # dashboard cannot quietly flip the kill into "on" — a future
        # phase will rework this once funded execution is green-lit.
        val = parse_bool(raw)
        if val:
            raise SettingsValidationError(
                "ENABLE_LIVE_TRADING=true is not allowed yet — live/funded "
                "execution is intentionally blocked in this build"
            )
        return False

    if key == "TOPSTEP_EXECUTION_CONFIRM":
        text = (str(raw) if raw is not None else "").strip()
        if not text:
            return "disabled"
        if text not in {"disabled", "DEMO_ONLY"}:
            raise SettingsValidationError(
                "TOPSTEP_EXECUTION_CONFIRM must be 'disabled' or 'DEMO_ONLY'"
            )
        return text

    if key == "ADMIN_USERNAME":
        text = (str(raw) if raw is not None else "").strip()
        if not text:
            raise SettingsValidationError("ADMIN_USERNAME cannot be empty")
        if len(text) > 128:
            raise SettingsValidationError(
                "ADMIN_USERNAME must be 128 characters or fewer"
            )
        return text

    if key == "ADMIN_PASSWORD_HASH":
        # The hash is opaque — produced by app.auth.hash_password. We
        # accept any string up to a generous ceiling and let the verifier
        # parse it. Empty clears the hash so the env-default plaintext
        # falls back in.
        text = str(raw) if raw is not None else ""
        if len(text) > 2048:
            raise SettingsValidationError("ADMIN_PASSWORD_HASH is too long")
        return text

    raise SettingsValidationError(f"unknown setting: {key}")


def generate_secret(length: int = 48) -> str:
    """Cryptographically strong URL-safe random secret."""
    return secrets.token_urlsafe(length)


# ---------------------------------------------------------------------
# Mapping between MANAGED_KEYS and Settings attributes
# ---------------------------------------------------------------------

_KEY_TO_ATTR: dict[str, str] = {
    "APP_HOST": "app_host",
    "APP_PORT": "app_port",
    "EXECUTION_MODE": "execution_mode",
    "BROKER_PROVIDER": "broker_provider",
    "SELECTED_ACCOUNT_ID": "selected_account_id",
    "TRADINGVIEW_WEBHOOK_SECRET": "webhook_secret",
    "ALLOWED_SYMBOLS": "allowed_symbols",
    "MAX_CONTRACTS_PER_TRADE": "max_contracts_per_trade",
    "STRATEGY_MANAGED_RISK": "strategy_managed_risk",
    "FIXED_CONTRACTS_PER_TRADE": "fixed_contracts_per_trade",
    "MAX_DAILY_LOSS": "max_daily_loss",
    "MAX_OPEN_POSITIONS": "max_open_positions",
    "ENABLE_LONGS": "enable_longs",
    "ENABLE_SHORTS": "enable_shorts",
    "DUPLICATE_ORDER_COOLDOWN_SECONDS": "duplicate_order_cooldown_seconds",
    "ENABLE_TIMEFRAME_LOCK": "enable_timeframe_lock",
    "ALLOWED_TIMEFRAMES": "allowed_timeframes",
    "TOPSTEP_USERNAME": "topstep_username",
    "TOPSTEP_API_KEY": "topstep_api_key",
    "TOPSTEP_ACCOUNT_ID": "topstep_account_id",
    "TOPSTEP_ENV": "topstep_env",
    "TOPSTEP_BASE_URL": "topstep_base_url",
    "TOPSTEP_WS_URL": "topstep_ws_url",
    "TOPSTEP_TOKEN": "topstep_token",
    "TOPSTEP_TOKEN_EXPIRES_AT": "topstep_token_expires_at",
    "ENABLE_TOPSTEP_ORDER_DRY_RUN": "enable_topstep_order_dry_run",
    "ENABLE_TOPSTEP_ORDER_EXECUTION": "enable_topstep_order_execution",
    "TOPSTEP_EXECUTION_CONFIRM": "topstep_execution_confirm",
    "ENABLE_LIVE_TRADING": "enable_live_trading",
    "ADMIN_USERNAME": "admin_username",
    "ADMIN_PASSWORD_HASH": "admin_password_hash",
}


def _read_settings_attr(settings: Settings, key: str) -> Any:
    return getattr(settings, _KEY_TO_ATTR[key])


def _write_settings_attr(settings: Settings, key: str, value: Any) -> None:
    setattr(settings, _KEY_TO_ATTR[key], value)


# ---------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------


_SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT NOT NULL
);
"""


class SettingsStore:
    """Tiny key/value store sitting on the same SQLite DB as the journal."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.executescript(_SCHEMA)
                conn.commit()
            finally:
                conn.close()

    # ----- raw key/value -----

    def get_setting(self, key: str) -> Optional[str]:
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    "SELECT value FROM settings WHERE key = ?", (key,)
                )
                row = cur.fetchone()
                return row["value"] if row else None
            finally:
                conn.close()

    def set_setting(self, key: str, value: str) -> None:
        if key not in MANAGED_KEYS:
            raise SettingsValidationError(f"unknown setting: {key}")
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    INSERT INTO settings (key, value, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET
                        value = excluded.value,
                        updated_at = excluded.updated_at
                    """,
                    (key, value, _utcnow_iso()),
                )
                conn.commit()
            finally:
                conn.close()

    def get_all_settings(self) -> dict[str, str]:
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute("SELECT key, value FROM settings")
                return {r["key"]: r["value"] for r in cur.fetchall()}
            finally:
                conn.close()

    # ----- typed helpers -----

    def update_typed(self, key: str, raw_value: Any) -> Any:
        """Validate + persist a single setting. Returns the coerced value."""
        coerced = coerce(key, raw_value)
        self.set_setting(key, serialize(key, coerced))
        return coerced

    # ----- env <-> store bootstrap -----

    def initialize_settings_from_env(self, settings: Settings) -> None:
        """For every managed key, if it's missing from SQLite, write the
        current env-driven value. Then overlay all stored values back
        onto `settings` so runtime reflects what the dashboard last
        saved."""
        for key in MANAGED_KEYS:
            stored = self.get_setting(key)
            if stored is None:
                env_value = _read_settings_attr(settings, key)
                self.set_setting(key, serialize(key, env_value))
                continue
            # Already in SQLite — overlay onto the live Settings object.
            try:
                coerced = coerce(key, stored)
            except SettingsValidationError:
                # Stored value is bad somehow — leave the env default.
                continue
            _write_settings_attr(settings, key, coerced)

    def apply_to_settings(
        self, settings: Settings, key: str, value: Any
    ) -> bool:
        """Apply a coerced value to the in-memory Settings. Returns True
        when the value can be honored without a restart, False if the
        change is persisted but a restart is needed for full effect."""
        _write_settings_attr(settings, key, value)
        # Update the legacy `broker` mirror so resolved_provider stays
        # consistent if anyone reads it.
        if key == "BROKER_PROVIDER":
            settings.broker = value
        return key in RUNTIME_APPLICABLE


def webhook_secret_preview(secret: str) -> str:
    if not secret:
        return ""
    if len(secret) < 6:
        return "set"
    return f"{secret[:3]}…{secret[-2:]}"
