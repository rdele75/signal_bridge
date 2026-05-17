"""Shared pytest fixtures.

Each test gets a fresh app bound to a temporary database / log path so the
suite is order-independent and never touches the user's real journal.
"""
from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path
from typing import Union

import pytest


SECRET = "test_secret_value_123456789"


def _build_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, provider: str = "paper"):
    """Construct a fresh FastAPI app bound to a temp DB/log under the given
    BROKER_PROVIDER."""
    db_path = tmp_path / "sb_test.db"
    log_path = tmp_path / "sb_test.log"

    monkeypatch.setenv("APP_HOST", "127.0.0.1")
    monkeypatch.setenv("APP_PORT", "8000")
    monkeypatch.setenv("EXECUTION_MODE", "paper")
    monkeypatch.setenv("BROKER_PROVIDER", provider)
    monkeypatch.setenv("BROKER", provider)
    monkeypatch.setenv("TRADINGVIEW_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("ALLOWED_SYMBOLS", "MES1!,MNQ1!")
    monkeypatch.setenv("MAX_CONTRACTS_PER_TRADE", "1")
    monkeypatch.setenv("MAX_DAILY_LOSS", "250")
    monkeypatch.setenv("MAX_OPEN_POSITIONS", "1")
    monkeypatch.setenv("ENABLE_LONGS", "true")
    monkeypatch.setenv("ENABLE_SHORTS", "true")
    monkeypatch.setenv("ENABLE_KILL_SWITCH", "true")
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    monkeypatch.setenv("LOG_PATH", str(log_path))
    monkeypatch.setenv("LOG_LEVEL", "WARNING")
    monkeypatch.setenv("DUPLICATE_ORDER_COOLDOWN_SECONDS", "60")
    # Point the symbol map at a non-existent file so tests don't depend on
    # whatever the user has in config/symbols.json.
    monkeypatch.setenv("SYMBOLS_MAP_PATH", str(tmp_path / "missing_symbols.json"))

    for mod in [m for m in list(sys.modules) if m.startswith("app")]:
        del sys.modules[mod]

    from app import config as config_mod  # noqa: E402

    config_mod.get_settings.cache_clear()

    from app.main import create_app  # noqa: E402

    return create_app()


@pytest.fixture
def app_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Default app fixture — paper provider."""
    yield _build_app(tmp_path, monkeypatch, provider="paper")


@pytest.fixture
def make_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Factory fixture for picking the broker provider per-test."""
    def _factory(provider: str = "paper"):
        return _build_app(tmp_path, monkeypatch, provider=provider)
    return _factory


@pytest.fixture
def client(app_env):
    from fastapi.testclient import TestClient

    with TestClient(app_env) as c:
        yield c


@pytest.fixture
def secret() -> str:
    return SECRET


def make_alert(
    *,
    secret: str = SECRET,
    symbol: str = "MES1!",
    action: str = "buy",
    contracts: Union[str, int, float] = "1",
    price: Union[str, int, float] = "5000.25",
    order_id: str = "test_order_001",
    **overrides,
) -> dict:
    base = {
        "secret": secret,
        "source": "tradingview",
        "strategy": "orb_200ema_confluence",
        "symbol": symbol,
        "exchange": "CME_MINI",
        "action": action,
        "contracts": contracts,
        "price": price,
        "position_size": "1",
        "market_position": "long",
        "order_id": order_id,
        "comment": "unit test",
        "bar_time": "2026-05-17T13:30:00Z",
        "fire_time": "2026-05-17T13:30:01Z",
    }
    base.update(overrides)
    return base
