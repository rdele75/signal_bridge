"""Tests for the SQLite-backed settings store + dashboard POSTs."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from .conftest import make_alert


# ---------- SettingsStore unit-ish tests ----------


def test_settings_table_initialized_on_startup(client):
    """Building the app should populate the settings table with the
    managed keys from .env."""
    store = client.app.state.settings_store
    rows = store.get_all_settings()
    for key in (
        "APP_HOST",
        "APP_PORT",
        "EXECUTION_MODE",
        "BROKER_PROVIDER",
        "TRADINGVIEW_WEBHOOK_SECRET",
        "ALLOWED_SYMBOLS",
        "MAX_CONTRACTS_PER_TRADE",
        "MAX_DAILY_LOSS",
        "MAX_OPEN_POSITIONS",
        "ENABLE_LONGS",
        "ENABLE_SHORTS",
        "DUPLICATE_ORDER_COOLDOWN_SECONDS",
    ):
        assert key in rows, f"settings table missing {key}"


def test_get_and_set_setting_roundtrip(client):
    store = client.app.state.settings_store
    store.set_setting("MAX_CONTRACTS_PER_TRADE", "5")
    assert store.get_setting("MAX_CONTRACTS_PER_TRADE") == "5"


def test_set_unknown_setting_rejected(client):
    from app.settings_store import SettingsValidationError

    store = client.app.state.settings_store
    with pytest.raises(SettingsValidationError):
        store.set_setting("NOT_A_REAL_SETTING", "x")


def test_parse_bool_helpers():
    from app.settings_store import SettingsValidationError, parse_bool

    for raw in ("true", "1", "yes", "on", True):
        assert parse_bool(raw) is True
    for raw in ("false", "0", "no", "off", False):
        assert parse_bool(raw) is False
    with pytest.raises(SettingsValidationError):
        parse_bool("maybe")


def test_parse_symbols_helper():
    from app.settings_store import parse_symbols

    assert parse_symbols("MES1!, MNQ1!,  ES1!") == ["MES1!", "MNQ1!", "ES1!"]
    assert parse_symbols("") == []
    assert parse_symbols(["A", " B "]) == ["A", "B"]


def test_coerce_accepts_live_mode():
    """``EXECUTION_MODE=live`` is accepted by the settings layer — the
    runtime live-gate check (`LIVE_TRADING_CONFIRM`, account ack,
    symbol/contract caps) is what actually decides whether an order is
    submitted. The broker form still rejects ``live`` so the only
    sanctioned arm path is /api/topstep/live-execution/enable."""
    from app.settings_store import coerce

    assert coerce("EXECUTION_MODE", "live") == "live"


# ---------- POST /settings/risk ----------


def test_post_settings_risk_saves_and_applies(client):
    r = client.post(
        "/settings/risk",
        data={
            "allowed_symbols": "MES1!,ES1!",
            "max_contracts_per_trade": "3",
            "max_daily_loss": "500",
            "max_open_positions": "2",
            "duplicate_order_cooldown_seconds": "90",
            "enable_longs": "true",
            "enable_shorts": "true",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "/settings/risk" in r.headers["location"]
    assert "Risk+settings+saved" in r.headers["location"]

    s = client.app.state.settings
    assert s.max_contracts_per_trade == 3
    assert s.max_daily_loss == 500.0
    assert s.max_open_positions == 2
    assert s.duplicate_order_cooldown_seconds == 90
    assert s.allowed_symbols == ["MES1!", "ES1!"]

    # Persisted in SQLite too.
    stored = client.app.state.settings_store.get_all_settings()
    assert stored["MAX_CONTRACTS_PER_TRADE"] == "3"
    assert stored["ALLOWED_SYMBOLS"] == "MES1!,ES1!"


def test_post_settings_risk_validation_error(client):
    r = client.post(
        "/settings/risk",
        data={
            "allowed_symbols": "MES1!",
            "max_contracts_per_trade": "not-a-number",
            "max_daily_loss": "0",
            "max_open_positions": "1",
            "duplicate_order_cooldown_seconds": "60",
            "enable_longs": "true",
            "enable_shorts": "true",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    # flash_kind=error encoded in the query string
    assert "flash_kind=error" in r.headers["location"]


def test_risk_engine_uses_updated_max_contracts(client):
    """After POSTing a smaller cap, a webhook with 1 contract still fits;
    after dropping cap below 1 we go the other way — bump cap to 5 and
    send 4."""
    client.post(
        "/settings/risk",
        data={
            "allowed_symbols": "MES1!,MNQ1!",
            "max_contracts_per_trade": "5",
            "max_daily_loss": "250",
            "max_open_positions": "2",
            "duplicate_order_cooldown_seconds": "60",
            "enable_longs": "true",
            "enable_shorts": "true",
        },
        follow_redirects=False,
    )
    r = client.post(
        "/webhooks/tradingview",
        json=make_alert(contracts="4", order_id="risk_runtime_1"),
    )
    body = r.json()
    assert body["accepted"] is True


def test_risk_engine_rejects_when_longs_disabled_at_runtime(client):
    client.post(
        "/settings/risk",
        data={
            "allowed_symbols": "MES1!,MNQ1!",
            "max_contracts_per_trade": "1",
            "max_daily_loss": "250",
            "max_open_positions": "1",
            "duplicate_order_cooldown_seconds": "60",
            # enable_longs intentionally omitted -> false
            "enable_shorts": "true",
        },
        follow_redirects=False,
    )
    r = client.post(
        "/webhooks/tradingview",
        json=make_alert(action="buy", order_id="no_longs_1"),
    )
    body = r.json()
    assert body["accepted"] is False
    assert body["rejection_reason"] == "longs_disabled"


# ---------- POST /settings/broker ----------


def test_post_settings_broker_paper(client):
    r = client.post(
        "/settings/broker",
        data={"broker_provider": "paper", "execution_mode": "paper"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "Broker+settings+saved" in r.headers["location"]
    assert client.app.state.settings.broker_provider == "paper"
    assert client.app.state.settings.execution_mode == "paper"


def test_post_settings_broker_rejects_live_mode(client):
    r = client.post(
        "/settings/broker",
        data={"broker_provider": "paper", "execution_mode": "live"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    loc = r.headers["location"]
    assert "flash_kind=error" in loc
    assert "live" in loc
    # settings should not have been changed.
    assert client.app.state.settings.execution_mode == "paper"


def test_post_settings_broker_rejects_unknown_provider(client):
    r = client.post(
        "/settings/broker",
        data={"broker_provider": "ibkr", "execution_mode": "paper"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "flash_kind=error" in r.headers["location"]


def test_api_status_reflects_updated_broker_provider(client):
    client.post(
        "/settings/broker",
        data={"broker_provider": "topstep", "execution_mode": "demo"},
        follow_redirects=False,
    )
    body = client.get("/api/status").json()
    assert body["broker_provider"] == "topstep"
    assert body["execution_mode"] == "demo"


def test_post_settings_broker_saves_selected_account_id(client):
    r = client.post(
        "/settings/broker",
        data={
            "broker_provider": "paper",
            "execution_mode": "paper",
            "selected_account_id": "PAPER-CUSTOM-9",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert client.app.state.settings.selected_account_id == "PAPER-CUSTOM-9"
    body = client.get("/api/status").json()
    assert body["selected_account_id"] == "PAPER-CUSTOM-9"


def test_post_settings_broker_blank_account_falls_back_to_default(client):
    r = client.post(
        "/settings/broker",
        data={
            "broker_provider": "paper",
            "execution_mode": "paper",
            "selected_account_id": "",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    body = client.get("/api/status").json()
    # Empty → resolves to per-provider default.
    assert body["selected_account_id"] == "PAPER-001"


# ---------- POST /tradingview/secret ----------


NEW_SECRET = "another_long_test_secret_value_xyz"


def test_tradingview_secret_input_uses_dark_theme_class(client):
    """The webhook-secret input on /tradingview must carry the
    dark-themed class so it doesn't render as a bright white box."""
    body = client.get("/tradingview").text
    # The current-secret readonly field.
    snippet_start = body.index('id="current_secret"')
    snippet = body[snippet_start: snippet_start + 400]
    assert 'class="dark-input"' in snippet
    # Copy button still wired.
    assert 'id="btn-copy-secret"' in body
    # The manual-set field is also dark-themed.
    snippet_start_2 = body.index('id="webhook_secret"')
    snippet_2 = body[snippet_start_2: snippet_start_2 + 400]
    assert 'class="dark-input"' in snippet_2


def test_tradingview_regenerated_secret_is_immediately_visible(client):
    """Regenerating the secret must reflect immediately in the readonly
    current-secret field on the next page render."""
    r = client.post("/tradingview/secret/regenerate", follow_redirects=False)
    assert r.status_code == 303
    new_secret = client.app.state.settings.webhook_secret
    assert new_secret
    body = client.get("/tradingview").text
    # The new secret must populate the readonly current-secret input.
    assert new_secret in body


def test_post_webhook_secret_persists(client):
    r = client.post(
        "/tradingview/secret",
        data={"webhook_secret": NEW_SECRET},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert client.app.state.settings.webhook_secret == NEW_SECRET
    assert (
        client.app.state.settings_store.get_setting("TRADINGVIEW_WEBHOOK_SECRET")
        == NEW_SECRET
    )


def test_post_webhook_secret_rejects_short(client):
    r = client.post(
        "/tradingview/secret",
        data={"webhook_secret": "short"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "flash_kind=error" in r.headers["location"]


def test_webhook_uses_updated_secret(client, secret):
    # Update secret to a known new value.
    client.post(
        "/tradingview/secret",
        data={"webhook_secret": NEW_SECRET},
        follow_redirects=False,
    )

    # Old secret -> rejected.
    r_old = client.post(
        "/webhooks/tradingview",
        json=make_alert(secret=secret, order_id="oldsec_1"),
    )
    body = r_old.json()
    assert body["accepted"] is False
    assert body["rejection_reason"] == "invalid_secret"

    # New secret -> accepted.
    r_new = client.post(
        "/webhooks/tradingview",
        json=make_alert(secret=NEW_SECRET, order_id="newsec_1"),
    )
    assert r_new.json()["accepted"] is True


def test_regenerate_webhook_secret(client):
    before = client.app.state.settings.webhook_secret
    r = client.post(
        "/tradingview/secret/regenerate", follow_redirects=False
    )
    assert r.status_code == 303
    after = client.app.state.settings.webhook_secret
    assert after and after != before
    assert len(after) >= 32  # token_urlsafe(48) is well over 32 chars


# ---------- Risk page form rendering ----------


def test_risk_page_renders_form(client):
    body = client.get("/settings/risk").text
    assert 'action="/settings/risk"' in body
    assert 'name="max_contracts_per_trade"' in body
    assert 'name="enable_longs"' in body
    # Allowed symbols has moved off the risk page (lives in a future
    # advanced settings page); the field must not be rendered here.
    assert 'name="allowed_symbols"' not in body


def test_broker_page_renders_form(client):
    body = client.get("/settings/broker").text
    assert 'action="/settings/broker"' in body
    assert 'name="broker_provider"' in body
    assert 'name="execution_mode"' in body


def test_tradingview_page_renders_secret_form(client):
    body = client.get("/tradingview").text
    assert 'action="/tradingview/secret"' in body
    assert 'name="webhook_secret"' in body
    assert 'action="/tradingview/secret/regenerate"' in body


# ---------- Settings persistence across app reload ----------


def test_stored_settings_override_env_on_reload(tmp_path, monkeypatch):
    """Set a value via the store, rebuild the app with the same DB and a
    different .env value, and confirm the stored value wins."""
    db_path = tmp_path / "sb_reload.db"
    log_path = tmp_path / "sb_reload.log"

    def _setenv(**kw):
        for k, v in kw.items():
            monkeypatch.setenv(k, v)

    _setenv(
        APP_HOST="127.0.0.1",
        APP_PORT="8000",
        EXECUTION_MODE="paper",
        BROKER_PROVIDER="paper",
        BROKER="paper",
        TRADINGVIEW_WEBHOOK_SECRET="initial_secret_value_12345",
        ALLOWED_SYMBOLS="MES1!,MNQ1!",
        MAX_CONTRACTS_PER_TRADE="1",
        MAX_DAILY_LOSS="250",
        MAX_OPEN_POSITIONS="1",
        ENABLE_LONGS="true",
        ENABLE_SHORTS="true",
        ENABLE_KILL_SWITCH="true",
        DATABASE_PATH=str(db_path),
        LOG_PATH=str(log_path),
        LOG_LEVEL="WARNING",
        DUPLICATE_ORDER_COOLDOWN_SECONDS="60",
        SYMBOLS_MAP_PATH=str(tmp_path / "missing.json"),
        ADMIN_AUTH_ENABLED="false",
    )

    import sys

    for mod in [m for m in list(sys.modules) if m.startswith("app")]:
        del sys.modules[mod]
    from app import config as config_mod  # noqa: E402

    config_mod.get_settings.cache_clear()
    from app.main import create_app  # noqa: E402

    app1 = create_app()
    with TestClient(app1) as c1:
        c1.post(
            "/settings/risk",
            data={
                "allowed_symbols": "ES1!",
                "max_contracts_per_trade": "7",
                "max_daily_loss": "999",
                "max_open_positions": "3",
                "duplicate_order_cooldown_seconds": "30",
                "enable_longs": "true",
                "enable_shorts": "true",
            },
            follow_redirects=False,
        )

    # Now wipe modules and rebuild with a *different* env, same DB.
    monkeypatch.setenv("MAX_CONTRACTS_PER_TRADE", "1")
    monkeypatch.setenv("ALLOWED_SYMBOLS", "MES1!")

    for mod in [m for m in list(sys.modules) if m.startswith("app")]:
        del sys.modules[mod]
    from app import config as config_mod  # noqa: E402

    config_mod.get_settings.cache_clear()
    from app.main import create_app  # noqa: E402

    app2 = create_app()
    s = app2.state.settings
    # Stored values from app1 should win over the new env defaults.
    assert s.max_contracts_per_trade == 7
    assert s.allowed_symbols == ["ES1!"]
