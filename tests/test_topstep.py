"""Tests for the scaffolded Topstep / TopstepX (ProjectX) adapter.

The Topstep adapter must never place real orders in this build. These
tests pin the safe behavior: missing-credentials envelope, scaffolded
envelope when credentials are configured, masked API key in the UI, and
the webhook routing safety net.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.execution.topstep import TopstepBroker
from app.schemas import NormalizedSignal

from .conftest import make_alert


def _signal(**overrides) -> NormalizedSignal:
    base = dict(
        source="tradingview",
        strategy="orb_200ema_confluence",
        symbol="MES1!",
        broker_symbol="MES",
        exchange="CME_MINI",
        action="BUY",
        contracts=1,
        price=5000.25,
        order_id="topstep_unit_1",
        comment="unit test",
        timeframe="1",
        raw={},
    )
    base.update(overrides)
    return NormalizedSignal(**base)


# ----------------------------------------------------------------------
# Adapter-level tests
# ----------------------------------------------------------------------


def test_test_connection_missing_credentials():
    """No username/api-key -> structured missing_credentials envelope."""
    broker = TopstepBroker(username="", api_key="")
    result = broker.test_connection()
    assert result["ok"] is False
    assert result["connected"] is False
    assert result["status"] == "missing_credentials"
    assert "Topstep username/API key not configured" in result["message"]
    creds = result["credentials"]
    assert creds["username_set"] is False
    assert creds["api_key_set"] is False
    assert creds["base_url"] == "https://api.topstepx.com"
    assert creds["ws_url"] == "https://rtc.topstepx.com"


def test_test_connection_configured_returns_scaffolded():
    """Credentials present -> scaffolded_not_connected, never connected."""
    broker = TopstepBroker(
        username="trader42",
        api_key="abcd1234efgh5678",
        account_id="PRACTICE-9001",
    )
    result = broker.test_connection()
    assert result["ok"] is False
    assert result["connected"] is False
    assert result["status"] == "scaffolded_not_connected"
    assert "real API auth not implemented yet" in result["message"]
    creds = result["credentials"]
    assert creds["username_set"] is True
    assert creds["api_key_set"] is True
    assert creds["account_id"] == "PRACTICE-9001"


def test_api_key_is_masked_in_status():
    """The credential summary must never echo the full API key back."""
    secret = "abcdefghijklmnop1234"
    broker = TopstepBroker(username="trader42", api_key=secret)
    creds = broker._credentials_summary()
    assert secret not in str(creds)
    assert creds["api_key_preview"] == "…1234"


def test_short_api_key_is_marked_configured_not_revealed():
    """Tiny keys are reported as 'configured' rather than exposing chars."""
    broker = TopstepBroker(username="trader42", api_key="ab")
    creds = broker._credentials_summary()
    assert creds["api_key_preview"] == "configured"
    assert "ab" not in creds["api_key_preview"]


def test_submit_market_order_refuses_with_clear_message():
    broker = TopstepBroker(
        username="trader42",
        api_key="abcd1234efgh5678",
        account_id="PRACTICE-9001",
    )
    result = broker.submit_market_order(_signal())
    assert result["ok"] is False
    assert result["accepted"] is False
    assert result["status"] == "scaffolded_not_connected"
    assert result["message"] == "Topstep order submission not implemented yet"
    assert result["symbol"] == "MES1!"


def test_execute_raises_topstep_not_implemented():
    """Webhook code path: the broker must raise NotImplementedError with
    a clearly labeled message so the handler can reject without no-op'ing
    a real order."""
    broker = TopstepBroker()
    with pytest.raises(NotImplementedError) as exc_info:
        broker.execute(_signal())
    assert "topstep_execution_not_implemented" in str(exc_info.value)


def test_authenticate_and_refresh_token_safe():
    broker = TopstepBroker(username="trader42", api_key="abcd1234efgh5678")
    auth = broker.authenticate()
    assert auth["ok"] is False
    assert auth["status"] == "scaffolded_not_connected"
    refresh = broker.refresh_token()
    assert refresh["ok"] is False
    assert refresh["status"] == "scaffolded_not_connected"


def test_authenticate_without_credentials_reports_missing():
    broker = TopstepBroker()
    auth = broker.authenticate()
    assert auth["status"] == "missing_credentials"


def test_read_only_methods_return_safe_envelopes():
    broker = TopstepBroker(username="trader42", api_key="abcd1234efgh5678")
    for method in (
        broker.get_accounts,
        broker.get_positions,
        broker.get_orders,
    ):
        result = method()
        assert result["ok"] is False
        assert result["not_implemented"] is True
        assert "scaffolded_not_connected" == result["status"]
    selected = broker.get_selected_account()
    assert selected["ok"] is False
    assert selected["not_implemented"] is True


# ----------------------------------------------------------------------
# HTTP-level tests against /api/broker/* with BROKER_PROVIDER=topstep
# ----------------------------------------------------------------------


def test_api_broker_status_for_topstep(make_app):
    app = make_app(provider="topstep")
    with TestClient(app) as c:
        r = c.get("/api/broker/status")
    assert r.status_code == 200
    body = r.json()
    assert body["provider"] == "topstep"
    assert body["broker_connected"] is False
    assert body["broker_provider"] == "topstep"


def test_api_broker_test_connection_for_topstep_missing_credentials(make_app):
    app = make_app(provider="topstep")
    with TestClient(app) as c:
        r = c.post("/api/broker/test-connection")
    # Documented envelope, not a server error.
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["status"] == "missing_credentials"
    assert body["provider"] == "topstep"


def test_api_broker_test_connection_for_topstep_configured(
    make_app, monkeypatch
):
    monkeypatch.setenv("TOPSTEP_USERNAME", "trader42")
    monkeypatch.setenv("TOPSTEP_API_KEY", "abcd1234efgh5678")
    monkeypatch.setenv("TOPSTEP_ACCOUNT_ID", "PRACTICE-9001")
    app = make_app(provider="topstep")
    with TestClient(app) as c:
        r = c.post("/api/broker/test-connection")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["status"] == "scaffolded_not_connected"
    # API key must not be echoed back in full.
    assert "abcd1234efgh5678" not in r.text


def test_api_broker_accounts_for_topstep_does_not_crash(make_app):
    app = make_app(provider="topstep")
    with TestClient(app) as c:
        r = c.get("/api/broker/accounts")
    assert r.status_code == 200
    body = r.json()
    assert body["provider"] == "topstep"
    assert body["accounts"] == []
    assert body["not_implemented"] is True


def test_api_broker_positions_and_orders_for_topstep(make_app):
    app = make_app(provider="topstep")
    with TestClient(app) as c:
        positions = c.get("/api/broker/positions").json()
        orders = c.get("/api/broker/orders").json()
    assert positions["positions"] == []
    assert positions["not_implemented"] is True
    assert orders["orders"] == []
    assert orders["not_implemented"] is True


# ----------------------------------------------------------------------
# Webhook routing safety
# ----------------------------------------------------------------------


def test_webhook_with_topstep_provider_does_not_silently_paper_execute(make_app):
    """Even with EXECUTION_MODE=paper-flavored env, a topstep provider must
    reject — never silently fill a paper order."""
    app = make_app(provider="topstep")
    with TestClient(app) as c:
        r = c.post(
            "/webhooks/tradingview",
            json=make_alert(order_id="topstep_safety_1"),
        )
    body = r.json()
    assert body["accepted"] is False
    assert body["decision"] == "rejected"
    assert "broker_not_implemented" in body["rejection_reason"]
    assert "topstep_execution_not_implemented" in body["rejection_reason"]


def test_paper_webhook_still_executes_when_provider_is_paper(make_app):
    """Regression guard: the paper webhook flow must still work end-to-end."""
    app = make_app(provider="paper")
    with TestClient(app) as c:
        r = c.post(
            "/webhooks/tradingview",
            json=make_alert(order_id="paper_regression_1"),
        )
    body = r.json()
    assert body["accepted"] is True
    assert body["execution"]["broker"] == "paper"


# ----------------------------------------------------------------------
# Settings persistence + masking on /settings/broker
# ----------------------------------------------------------------------


def test_post_settings_broker_persists_topstep_fields(client):
    r = client.post(
        "/settings/broker",
        data={
            "broker_provider": "topstep",
            "execution_mode": "demo",
            "selected_account_id": "",
            "topstep_username": "trader42",
            "topstep_api_key": "abcd1234efgh5678",
            "topstep_account_id": "PRACTICE-9001",
            "topstep_env": "demo",
            "topstep_base_url": "https://api.topstepx.com",
            "topstep_ws_url": "https://rtc.topstepx.com",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "Broker+settings+saved" in r.headers["location"]
    s = client.app.state.settings
    assert s.topstep_username == "trader42"
    assert s.topstep_api_key == "abcd1234efgh5678"
    assert s.topstep_account_id == "PRACTICE-9001"
    assert s.topstep_env == "demo"
    assert s.topstep_base_url == "https://api.topstepx.com"
    assert s.topstep_ws_url == "https://rtc.topstepx.com"
    stored = client.app.state.settings_store.get_all_settings()
    assert stored["TOPSTEP_USERNAME"] == "trader42"
    assert stored["TOPSTEP_API_KEY"] == "abcd1234efgh5678"


def test_post_settings_broker_keeps_existing_api_key_when_blank_sentinel(client):
    """Posting without the topstep_api_key field (i.e. user didn't type
    anything and the form sentinel survives) must NOT clear the saved key."""
    # First save a key.
    client.post(
        "/settings/broker",
        data={
            "broker_provider": "paper",
            "execution_mode": "paper",
            "topstep_username": "trader42",
            "topstep_api_key": "abcd1234efgh5678",
            "topstep_account_id": "PRACTICE-9001",
            "topstep_env": "demo",
            "topstep_base_url": "https://api.topstepx.com",
            "topstep_ws_url": "https://rtc.topstepx.com",
        },
        follow_redirects=False,
    )
    # Now save again without including topstep_api_key.
    client.post(
        "/settings/broker",
        data={
            "broker_provider": "paper",
            "execution_mode": "paper",
            "topstep_username": "trader42",
            "topstep_account_id": "PRACTICE-9001",
            "topstep_env": "demo",
            "topstep_base_url": "https://api.topstepx.com",
            "topstep_ws_url": "https://rtc.topstepx.com",
        },
        follow_redirects=False,
    )
    assert client.app.state.settings.topstep_api_key == "abcd1234efgh5678"


def test_settings_broker_page_does_not_leak_full_api_key(client):
    client.post(
        "/settings/broker",
        data={
            "broker_provider": "paper",
            "execution_mode": "paper",
            "topstep_username": "trader42",
            "topstep_api_key": "abcd1234efgh5678",
            "topstep_account_id": "PRACTICE-9001",
            "topstep_env": "demo",
            "topstep_base_url": "https://api.topstepx.com",
            "topstep_ws_url": "https://rtc.topstepx.com",
        },
        follow_redirects=False,
    )
    body = client.get("/settings/broker").text
    assert "abcd1234efgh5678" not in body
    # Last-4 preview is okay to show.
    assert "…5678" in body


def test_post_settings_broker_rejects_live_topstep_env(client):
    r = client.post(
        "/settings/broker",
        data={
            "broker_provider": "paper",
            "execution_mode": "paper",
            "topstep_username": "trader42",
            "topstep_account_id": "PRACTICE-9001",
            "topstep_env": "live",
            "topstep_base_url": "https://api.topstepx.com",
            "topstep_ws_url": "https://rtc.topstepx.com",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "flash_kind=error" in r.headers["location"]


def test_post_settings_broker_rejects_bad_base_url(client):
    r = client.post(
        "/settings/broker",
        data={
            "broker_provider": "paper",
            "execution_mode": "paper",
            "topstep_username": "trader42",
            "topstep_account_id": "PRACTICE-9001",
            "topstep_env": "demo",
            "topstep_base_url": "not-a-url",
            "topstep_ws_url": "https://rtc.topstepx.com",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "flash_kind=error" in r.headers["location"]
