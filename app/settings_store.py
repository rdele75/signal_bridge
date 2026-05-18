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
    "MAX_DAILY_LOSS",
    "MAX_OPEN_POSITIONS",
    "ENABLE_LONGS",
    "ENABLE_SHORTS",
    "DUPLICATE_ORDER_COOLDOWN_SECONDS",
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
        "MAX_DAILY_LOSS",
        "MAX_OPEN_POSITIONS",
        "ENABLE_LONGS",
        "ENABLE_SHORTS",
        "DUPLICATE_ORDER_COOLDOWN_SECONDS",
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

    if key in {"MAX_CONTRACTS_PER_TRADE", "MAX_OPEN_POSITIONS"}:
        return parse_int(raw, min_value=1)

    if key == "MAX_DAILY_LOSS":
        return parse_float(raw, min_value=0.0)

    if key == "DUPLICATE_ORDER_COOLDOWN_SECONDS":
        return parse_int(raw, min_value=0)

    if key in {"ENABLE_LONGS", "ENABLE_SHORTS"}:
        return parse_bool(raw)

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
    "MAX_DAILY_LOSS": "max_daily_loss",
    "MAX_OPEN_POSITIONS": "max_open_positions",
    "ENABLE_LONGS": "enable_longs",
    "ENABLE_SHORTS": "enable_shorts",
    "DUPLICATE_ORDER_COOLDOWN_SECONDS": "duplicate_order_cooldown_seconds",
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
