"""Tests for the Topstep / TopstepX (ProjectX) adapter.

This phase implements auth + active-account discovery only — every test
that exercises HTTP monkey-patches ``TopstepBroker._post_json`` so the
suite never hits topstepx.com. Order placement must still refuse and the
webhook flow for ``BROKER_PROVIDER=topstep`` must still reject without
silently routing to paper.
"""
from __future__ import annotations

from typing import Any

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


def _login_ok(token: str = "JWT.LOGIN.TOKEN") -> dict[str, Any]:
    return {
        "token": token,
        "success": True,
        "errorCode": 0,
        "errorMessage": None,
    }


def _accounts_ok(*accounts) -> dict[str, Any]:
    return {
        "success": True,
        "errorCode": 0,
        "errorMessage": None,
        "accounts": list(accounts),
    }


def _acct(
    id_: str = "ACCT-1",
    name: str = "Practice",
    balance: float = 50000.0,
    can_trade: bool = True,
    is_visible: bool = True,
) -> dict[str, Any]:
    return {
        "id": id_,
        "name": name,
        "balance": balance,
        "canTrade": can_trade,
        "isVisible": is_visible,
    }


# ----------------------------------------------------------------------
# Adapter: credential / mask behavior
# ----------------------------------------------------------------------


def test_test_connection_missing_credentials():
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


def test_api_key_is_masked_in_status():
    secret = "abcdefghijklmnop1234"
    broker = TopstepBroker(username="trader42", api_key=secret)
    creds = broker._credentials_summary()
    assert secret not in str(creds)
    assert creds["api_key_preview"] == "…1234"


def test_short_api_key_is_marked_configured_not_revealed():
    broker = TopstepBroker(username="trader42", api_key="ab")
    creds = broker._credentials_summary()
    assert creds["api_key_preview"] == "configured"
    assert "ab" not in creds["api_key_preview"]


def test_token_is_masked_in_credentials_summary():
    secret_token = "supersecret.jwt.token.value"
    broker = TopstepBroker(
        username="trader42",
        api_key="abcd1234efgh5678",
        token=secret_token,
        token_expires_at="2099-01-01T00:00:00+00:00",
    )
    creds = broker._credentials_summary()
    assert secret_token not in str(creds)
    assert creds["token_cached"] is True
    assert creds["token_preview"].startswith("…")


# ----------------------------------------------------------------------
# Adapter: authentication
# ----------------------------------------------------------------------


def test_authenticate_without_credentials_reports_missing():
    broker = TopstepBroker()
    auth = broker.authenticate()
    assert auth["status"] == "missing_credentials"
    assert auth["ok"] is False
    assert auth["connected"] is False


def test_authenticate_success_stores_token(monkeypatch):
    calls: list[tuple[str, dict[str, Any], bool]] = []
    sink_calls: list[tuple[str, str]] = []

    def fake_post(self, path, payload, *, auth=False):
        calls.append((path, payload, auth))
        return 200, _login_ok("JWT.ABC.123")

    monkeypatch.setattr(TopstepBroker, "_post_json", fake_post)
    broker = TopstepBroker(
        username="trader42",
        api_key="abcd1234efgh5678",
        token_sink=lambda t, e: sink_calls.append((t, e)),
    )
    result = broker.authenticate()
    assert result["ok"] is True
    assert result["status"] == "authenticated"
    assert broker.token == "JWT.ABC.123"
    assert broker.token_expires_at
    # Token was persisted via the sink.
    assert sink_calls and sink_calls[0][0] == "JWT.ABC.123"
    # Token must NOT appear in the public envelope.
    assert "JWT.ABC.123" not in str(result)
    # Verify the call shape against the ProjectX contract.
    assert calls[0][0] == "/api/Auth/loginKey"
    assert calls[0][1] == {
        "userName": "trader42",
        "apiKey": "abcd1234efgh5678",
    }
    assert calls[0][2] is False


def test_authenticate_failure_returns_auth_failed(monkeypatch):
    def fake_post(self, path, payload, *, auth=False):
        return 401, {
            "token": "",
            "success": False,
            "errorCode": 3,
            "errorMessage": "invalid api key",
        }

    monkeypatch.setattr(TopstepBroker, "_post_json", fake_post)
    broker = TopstepBroker(username="trader42", api_key="badkey1234")
    result = broker.authenticate()
    assert result["ok"] is False
    assert result["status"] == "auth_failed"
    assert result["error_code"] == 3
    assert result["error_message"] == "invalid api key"
    # Token must not have been stored on failure.
    assert broker.token == ""


def test_authenticate_network_error_returns_network_envelope(monkeypatch):
    def fake_post(self, path, payload, *, auth=False):
        return 0, "network_error: ConnectError"

    monkeypatch.setattr(TopstepBroker, "_post_json", fake_post)
    broker = TopstepBroker(username="trader42", api_key="abcd1234efgh5678")
    result = broker.authenticate()
    assert result["ok"] is False
    assert result["status"] == "network_error"
    assert "ConnectError" in result["message"]


# ----------------------------------------------------------------------
# Adapter: account discovery
# ----------------------------------------------------------------------


def test_get_accounts_authenticates_then_calls_with_bearer(monkeypatch):
    calls: list[tuple[str, dict[str, Any], bool, dict[str, str] | None]] = []

    def fake_post(self, path, payload, *, auth=False):
        if path == "/api/Auth/loginKey":
            return 200, _login_ok("JWT.AUTH.0")
        calls.append((path, payload, auth, self._auth_headers_or_none()))
        return 200, _accounts_ok(_acct("ACCT-1", "Practice", 50000.0))

    monkeypatch.setattr(TopstepBroker, "_post_json", fake_post)
    broker = TopstepBroker(username="trader42", api_key="abcd1234efgh5678")
    result = broker.get_accounts()
    assert result["ok"] is True
    assert result["accounts"][0]["id"] == "ACCT-1"
    assert result["accounts"][0]["account_id"] == "ACCT-1"
    assert result["accounts"][0]["can_trade"] is True
    assert result["accounts"][0]["is_visible"] is True
    # The accounts call must have been made with the bearer header.
    accounts_call = calls[0]
    assert accounts_call[0] == "/api/Account/search"
    assert accounts_call[1] == {"onlyActiveAccounts": True}
    assert accounts_call[2] is True
    assert accounts_call[3] == {"Authorization": "Bearer JWT.AUTH.0"}


def test_get_accounts_missing_credentials_returns_safe_envelope():
    broker = TopstepBroker()
    result = broker.get_accounts()
    assert result["ok"] is False
    assert result["status"] == "missing_credentials"
    assert result["accounts"] == []


def test_get_accounts_handles_unexpected_shape(monkeypatch):
    def fake_post(self, path, payload, *, auth=False):
        if path == "/api/Auth/loginKey":
            return 200, _login_ok()
        # Missing "accounts" entirely — should not crash.
        return 200, {"success": True, "errorCode": 0}

    monkeypatch.setattr(TopstepBroker, "_post_json", fake_post)
    broker = TopstepBroker(username="trader42", api_key="abcd1234efgh5678")
    result = broker.get_accounts()
    assert result["ok"] is True
    assert result["accounts"] == []


def test_get_selected_account_matches_configured_id(monkeypatch):
    def fake_post(self, path, payload, *, auth=False):
        if path == "/api/Auth/loginKey":
            return 200, _login_ok()
        return 200, _accounts_ok(
            _acct("ACCT-1", "Practice", 50000.0),
            _acct("ACCT-2", "Combine", 100000.0),
        )

    monkeypatch.setattr(TopstepBroker, "_post_json", fake_post)
    broker = TopstepBroker(
        username="trader42",
        api_key="abcd1234efgh5678",
        account_id="ACCT-2",
    )
    result = broker.get_selected_account()
    assert result["ok"] is True
    assert result["selected_account_id"] == "ACCT-2"
    assert result["account"]["name"] == "Combine"


def test_get_selected_account_matches_numeric_id_via_string(monkeypatch):
    """ProjectX returns integer account ids; the operator-configured
    ``TOPSTEP_ACCOUNT_ID`` is persisted as a string. They must match."""
    def fake_post(self, path, payload, *, auth=False):
        if path == "/api/Auth/loginKey":
            return 200, _login_ok()
        return 200, _accounts_ok(
            {"id": 5001, "name": "Practice 1", "balance": 50000.0,
             "canTrade": True, "isVisible": True},
            {"id": 6002, "name": "Combine 2", "balance": 150000.0,
             "canTrade": True, "isVisible": True},
        )

    monkeypatch.setattr(TopstepBroker, "_post_json", fake_post)
    broker = TopstepBroker(
        username="trader42",
        api_key="abcd1234efgh5678",
        account_id="6002",  # numeric-looking string
    )
    result = broker.get_selected_account()
    assert result["ok"] is True
    assert result["selected_account_id"] == "6002"
    assert result["account"]["id"] == 6002
    assert result["account"]["id_str"] == "6002"
    assert result["account"]["name"] == "Combine 2"


def test_get_selected_account_matches_padded_string_id(monkeypatch):
    """Whitespace from a dashboard paste must not break id matching."""
    def fake_post(self, path, payload, *, auth=False):
        if path == "/api/Auth/loginKey":
            return 200, _login_ok()
        return 200, _accounts_ok(
            {"id": 5001, "name": "Practice", "balance": 50000.0,
             "canTrade": True, "isVisible": True},
        )

    monkeypatch.setattr(TopstepBroker, "_post_json", fake_post)
    broker = TopstepBroker(
        username="trader42",
        api_key="abcd1234efgh5678",
        account_id=" 5001 ",
    )
    result = broker.get_selected_account()
    assert result["ok"] is True
    assert result["account"]["id"] == 5001


def test_account_search_returns_parsed_balance_canTrade_isVisible(monkeypatch):
    """Parsed account rows expose balance, can_trade, is_visible."""
    def fake_post(self, path, payload, *, auth=False):
        if path == "/api/Auth/loginKey":
            return 200, _login_ok()
        return 200, _accounts_ok(
            {"id": 9001, "name": "Funded", "balance": 125_000.0,
             "canTrade": True, "isVisible": False},
        )

    monkeypatch.setattr(TopstepBroker, "_post_json", fake_post)
    broker = TopstepBroker(username="trader42", api_key="abcd1234efgh5678")
    result = broker.get_accounts()
    assert result["ok"] is True
    acct = result["accounts"][0]
    assert acct["id"] == 9001
    assert acct["balance"] == 125_000.0
    assert acct["can_trade"] is True
    assert acct["is_visible"] is False


# ----------------------------------------------------------------------
# Adapter: read-only positions / orders (scaffolded not_implemented)
# ----------------------------------------------------------------------


def test_get_positions_returns_safe_not_implemented_envelope():
    broker = TopstepBroker(
        username="trader42",
        api_key="abcd1234efgh5678",
        account_id="5001",
    )
    result = broker.get_positions()
    assert result["ok"] is False
    assert result["provider"] == "topstep"
    assert result["status"] == "not_implemented"
    assert result["not_implemented"] is True
    assert result["positions"] == []
    assert "not implemented" in result["message"].lower()
    assert result["selected_account_id"] == "5001"


def test_get_orders_returns_safe_not_implemented_envelope():
    broker = TopstepBroker(
        username="trader42",
        api_key="abcd1234efgh5678",
        account_id="5001",
    )
    result = broker.get_orders()
    assert result["ok"] is False
    assert result["provider"] == "topstep"
    assert result["status"] == "not_implemented"
    assert result["not_implemented"] is True
    assert result["orders"] == []
    assert "not implemented" in result["message"].lower()


def test_get_positions_and_orders_never_call_http(monkeypatch):
    """While positions/orders are scaffolded, they must not hit the
    network. A real HTTP call would be a regression — the wiring is
    deliberately read-only-not-yet."""
    calls: list[str] = []

    def fake_post(self, path, payload, *, auth=False):
        calls.append(path)
        return 0, "network_error"

    monkeypatch.setattr(TopstepBroker, "_post_json", fake_post)
    broker = TopstepBroker(
        username="trader42",
        api_key="abcd1234efgh5678",
        account_id="5001",
    )
    broker.get_positions()
    broker.get_orders()
    assert calls == []


# ----------------------------------------------------------------------
# test_connection includes the selected-account snapshot
# ----------------------------------------------------------------------


def test_test_connection_includes_selected_account_snapshot(monkeypatch):
    def fake_post(self, path, payload, *, auth=False):
        if path == "/api/Auth/loginKey":
            return 200, _login_ok()
        return 200, _accounts_ok(
            {"id": 5001, "name": "Practice", "balance": 50000.0,
             "canTrade": True, "isVisible": True},
            {"id": 6002, "name": "Combine",  "balance": 150_000.0,
             "canTrade": True, "isVisible": True},
        )

    monkeypatch.setattr(TopstepBroker, "_post_json", fake_post)
    broker = TopstepBroker(
        username="trader42",
        api_key="abcd1234efgh5678",
        account_id="6002",
    )
    result = broker.test_connection()
    assert result["ok"] is True
    assert result["selected_account"] is not None
    assert result["selected_account"]["id"] == 6002
    assert result["selected_account"]["name"] == "Combine"
    assert result["selected_account"]["balance"] == 150_000.0


def test_get_selected_account_clear_when_not_set(monkeypatch):
    def fake_post(self, path, payload, *, auth=False):
        if path == "/api/Auth/loginKey":
            return 200, _login_ok()
        return 200, _accounts_ok(_acct("ACCT-1"))

    monkeypatch.setattr(TopstepBroker, "_post_json", fake_post)
    broker = TopstepBroker(
        username="trader42", api_key="abcd1234efgh5678", account_id=""
    )
    result = broker.get_selected_account()
    assert result["ok"] is False
    assert result["status"] == "no_selected_account"
    assert "SELECTED_ACCOUNT_ID" in result["message"]


# ----------------------------------------------------------------------
# Adapter: test_connection covers the full success path
# ----------------------------------------------------------------------


def test_test_connection_authenticated_with_accounts(monkeypatch):
    def fake_post(self, path, payload, *, auth=False):
        if path == "/api/Auth/loginKey":
            return 200, _login_ok()
        return 200, _accounts_ok(_acct("ACCT-1"), _acct("ACCT-2"))

    monkeypatch.setattr(TopstepBroker, "_post_json", fake_post)
    broker = TopstepBroker(
        username="trader42",
        api_key="abcd1234efgh5678",
        account_id="ACCT-1",
    )
    result = broker.test_connection()
    assert result["ok"] is True
    assert result["connected"] is True
    assert result["status"] == "ok"
    assert result["accounts_count"] == 2
    assert result["selected_account_id"] == "ACCT-1"


def test_test_connection_no_accounts_status(monkeypatch):
    def fake_post(self, path, payload, *, auth=False):
        if path == "/api/Auth/loginKey":
            return 200, _login_ok()
        return 200, _accounts_ok()

    monkeypatch.setattr(TopstepBroker, "_post_json", fake_post)
    broker = TopstepBroker(username="trader42", api_key="abcd1234efgh5678")
    result = broker.test_connection()
    assert result["ok"] is True
    assert result["connected"] is True
    assert result["status"] == "no_accounts"
    assert result["accounts_count"] == 0


# ----------------------------------------------------------------------
# Adapter: order placement stays disabled
# ----------------------------------------------------------------------


def test_submit_market_order_refuses_with_topstep_execution_not_enabled():
    broker = TopstepBroker(
        username="trader42",
        api_key="abcd1234efgh5678",
        account_id="ACCT-1",
    )
    result = broker.submit_market_order(_signal())
    assert result["ok"] is False
    assert result["accepted"] is False
    assert result["status"] == "topstep_execution_not_enabled"
    assert "order submission is disabled" in result["message"]
    assert result["symbol"] == "MES1!"


def test_flatten_and_cancel_disabled():
    broker = TopstepBroker(username="trader42", api_key="abcd1234efgh5678")
    flat = broker.flatten_position()
    assert flat["status"] == "topstep_execution_not_enabled"
    cancel = broker.cancel_all_orders()
    assert cancel["status"] == "topstep_execution_not_enabled"


def test_execute_raises_topstep_execution_not_enabled():
    broker = TopstepBroker()
    with pytest.raises(NotImplementedError) as exc_info:
        broker.execute(_signal())
    assert "topstep_execution_not_enabled" in str(exc_info.value)


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
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["status"] == "missing_credentials"
    assert body["provider"] == "topstep"


def test_api_broker_test_connection_for_topstep_with_mocked_auth(
    make_app, monkeypatch
):
    """Configured credentials + mocked HTTP -> full success envelope."""
    monkeypatch.setenv("TOPSTEP_USERNAME", "trader42")
    monkeypatch.setenv("TOPSTEP_API_KEY", "abcd1234efgh5678")
    monkeypatch.setenv("TOPSTEP_ACCOUNT_ID", "ACCT-1")

    app = make_app(provider="topstep")
    # `make_app` reloads `app.*` modules — patch the freshly-imported
    # TopstepBroker class, not the stale one cached at the top of this file.
    from app.execution.topstep import TopstepBroker as FreshTopstepBroker

    def fake_post(self, path, payload, *, auth=False):
        if path == "/api/Auth/loginKey":
            return 200, _login_ok("JWT.LIVE.AUTH")
        return 200, _accounts_ok(_acct("ACCT-1"))

    monkeypatch.setattr(FreshTopstepBroker, "_post_json", fake_post)

    with TestClient(app) as c:
        r = c.post("/api/broker/test-connection")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["status"] == "ok"
    assert body["connected"] is True
    assert body["accounts_count"] == 1
    # API key + token must not be echoed back in full.
    assert "abcd1234efgh5678" not in r.text
    assert "JWT.LIVE.AUTH" not in r.text


def test_api_broker_accounts_for_topstep_missing_creds(make_app):
    app = make_app(provider="topstep")
    with TestClient(app) as c:
        r = c.get("/api/broker/accounts")
    assert r.status_code == 200
    body = r.json()
    assert body["provider"] == "topstep"
    assert body["accounts"] == []
    assert body["status"] == "missing_credentials"


def test_api_broker_positions_and_orders_for_topstep(make_app):
    app = make_app(provider="topstep")
    with TestClient(app) as c:
        positions = c.get("/api/broker/positions").json()
        orders = c.get("/api/broker/orders").json()
    assert positions["positions"] == []
    assert positions["not_implemented"] is True
    assert positions["status"] == "not_implemented"
    assert positions["provider"] == "topstep"
    assert "not implemented" in positions["message"].lower()
    assert orders["orders"] == []
    assert orders["not_implemented"] is True
    assert orders["status"] == "not_implemented"
    assert orders["provider"] == "topstep"
    assert "not implemented" in orders["message"].lower()


def test_api_broker_status_for_topstep_with_mocked_auth_exposes_account_details(
    make_app, monkeypatch
):
    """With auth + account discovery succeeding, /api/broker/status must
    surface the selected account's name, balance, canTrade, isVisible,
    and the token-cache state — without ever echoing the JWT itself."""
    monkeypatch.setenv("TOPSTEP_USERNAME", "trader42")
    monkeypatch.setenv("TOPSTEP_API_KEY", "abcd1234efgh5678")
    monkeypatch.setenv("TOPSTEP_ACCOUNT_ID", "6002")

    app = make_app(provider="topstep")
    from app.execution.topstep import TopstepBroker as FreshTopstepBroker

    def fake_post(self, path, payload, *, auth=False):
        if path == "/api/Auth/loginKey":
            return 200, _login_ok("JWT.STATUS.AUTH")
        return 200, _accounts_ok(
            {"id": 5001, "name": "Practice", "balance": 50000.0,
             "canTrade": True, "isVisible": True},
            {"id": 6002, "name": "Combine", "balance": 150_000.0,
             "canTrade": True, "isVisible": True},
        )

    monkeypatch.setattr(FreshTopstepBroker, "_post_json", fake_post)

    with TestClient(app) as c:
        r = c.get("/api/broker/status")
    assert r.status_code == 200
    body = r.json()
    assert body["provider"] == "topstep"
    assert body["broker_connected"] is True
    assert body["selected_account_id"] == "6002"
    assert body["selected_account_name"] == "Combine"
    assert body["selected_account"]["id"] == 6002
    assert body["balance"] == 150_000.0
    assert body["can_trade"] is True
    assert body["is_visible"] is True
    assert body["token_cached"] is True
    assert body["auth_status"] == "ok"
    # Token expiry is exposed only as a date/time prefix, never the
    # raw token itself.
    assert "JWT.STATUS.AUTH" not in r.text
    # abcd1234efgh5678 must not appear in full — the payload includes
    # the credentials summary which keeps only the …5678 preview.
    assert "abcd1234efgh5678" not in r.text


def test_api_broker_status_for_topstep_missing_credentials_safe(make_app):
    app = make_app(provider="topstep")
    with TestClient(app) as c:
        r = c.get("/api/broker/status")
    assert r.status_code == 200
    body = r.json()
    assert body["provider"] == "topstep"
    assert body["broker_connected"] is False
    assert body["status"] == "missing_credentials"
    assert body["selected_account"] is None
    assert body["balance"] is None
    assert body["can_trade"] is None
    assert body["is_visible"] is None
    assert body["token_cached"] is False


# ----------------------------------------------------------------------
# /api/topstep/* endpoints
# ----------------------------------------------------------------------


def test_api_topstep_authenticate_requires_auth(auth_app_env):
    with TestClient(auth_app_env) as c:
        r = c.post("/api/topstep/authenticate")
    assert r.status_code == 401


def test_api_topstep_accounts_requires_auth(auth_app_env):
    with TestClient(auth_app_env) as c:
        r = c.get("/api/topstep/accounts")
    assert r.status_code == 401


def test_api_topstep_select_account_requires_auth(auth_app_env):
    with TestClient(auth_app_env) as c:
        r = c.post("/api/topstep/select-account", data={"account_id": "ACCT-1"})
    assert r.status_code == 401


def test_api_topstep_authenticate_persists_token(client, monkeypatch):
    """authenticate persists the token (and expiry) to SQLite — even when
    BROKER_PROVIDER is still paper."""
    settings = client.app.state.settings
    store = client.app.state.settings_store
    store.apply_to_settings(
        settings,
        "TOPSTEP_USERNAME",
        store.update_typed("TOPSTEP_USERNAME", "trader42"),
    )
    store.apply_to_settings(
        settings,
        "TOPSTEP_API_KEY",
        store.update_typed("TOPSTEP_API_KEY", "abcd1234efgh5678"),
    )

    # The `client` fixture reloads `app.*` — patch the freshly-imported class.
    from app.execution.topstep import TopstepBroker as FreshTopstepBroker

    def fake_post(self, path, payload, *, auth=False):
        return 200, _login_ok("JWT.PERSIST.TOKEN")

    monkeypatch.setattr(FreshTopstepBroker, "_post_json", fake_post)

    r = client.post("/api/topstep/authenticate")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["status"] == "authenticated"
    # Token must never leak into the API response.
    assert "JWT.PERSIST.TOKEN" not in r.text

    stored = store.get_all_settings()
    assert stored["TOPSTEP_TOKEN"] == "JWT.PERSIST.TOKEN"
    assert stored["TOPSTEP_TOKEN_EXPIRES_AT"]


def test_api_topstep_accounts_returns_parsed_accounts(client, monkeypatch):
    settings = client.app.state.settings
    store = client.app.state.settings_store
    store.apply_to_settings(
        settings, "TOPSTEP_USERNAME", store.update_typed("TOPSTEP_USERNAME", "trader42")
    )
    store.apply_to_settings(
        settings, "TOPSTEP_API_KEY", store.update_typed("TOPSTEP_API_KEY", "abcd1234efgh5678")
    )

    from app.execution.topstep import TopstepBroker as FreshTopstepBroker

    def fake_post(self, path, payload, *, auth=False):
        if path == "/api/Auth/loginKey":
            return 200, _login_ok()
        return 200, _accounts_ok(
            _acct("ACCT-1", "Practice", 50000.0),
            _acct("ACCT-2", "Combine", 100000.0),
        )

    monkeypatch.setattr(FreshTopstepBroker, "_post_json", fake_post)

    r = client.get("/api/topstep/accounts")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert len(body["accounts"]) == 2
    assert body["accounts"][0]["id"] == "ACCT-1"
    assert body["accounts"][1]["name"] == "Combine"


def test_api_topstep_select_account_saves_both_keys(client):
    r = client.post(
        "/api/topstep/select-account",
        data={"account_id": "PRACTICE-9001"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["selected_account_id"] == "PRACTICE-9001"
    assert body["topstep_account_id"] == "PRACTICE-9001"

    stored = client.app.state.settings_store.get_all_settings()
    assert stored["SELECTED_ACCOUNT_ID"] == "PRACTICE-9001"
    assert stored["TOPSTEP_ACCOUNT_ID"] == "PRACTICE-9001"
    assert client.app.state.settings.selected_account_id == "PRACTICE-9001"
    assert client.app.state.settings.topstep_account_id == "PRACTICE-9001"


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
    assert "topstep_execution_not_enabled" in body["rejection_reason"]


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


# ----------------------------------------------------------------------
# Whitespace stripping + exact ProjectX request shape
# ----------------------------------------------------------------------


def test_topstep_broker_strips_whitespace_on_load():
    """Stray newlines/spaces from a dashboard paste must not reach the wire."""
    broker = TopstepBroker(
        username="  trader42 \n",
        api_key=" abcd1234efgh5678\r\n ",
        account_id="  ACCT-1 ",
    )
    assert broker.username == "trader42"
    assert broker.api_key == "abcd1234efgh5678"
    assert broker.account_id == "ACCT-1"


def test_authenticate_strips_credentials_before_send(monkeypatch):
    """If a caller mutated the attrs directly post-init, the request payload
    must still go out trimmed."""
    captured: dict[str, Any] = {}

    def fake_post(self, path, payload, *, auth=False):
        captured["path"] = path
        captured["payload"] = payload
        return 200, _login_ok("JWT.TRIMMED")

    monkeypatch.setattr(TopstepBroker, "_post_json", fake_post)
    broker = TopstepBroker(username="trader42", api_key="abcd1234efgh5678")
    # Simulate a stale, whitespace-padded mutation.
    broker.username = "  trader42  "
    broker.api_key = " abcd1234efgh5678\n"
    broker.authenticate()
    assert captured["payload"] == {
        "userName": "trader42",
        "apiKey": "abcd1234efgh5678",
    }


def test_settings_store_strips_topstep_api_key_on_save(client):
    """Dashboard paste with trailing whitespace must persist trimmed."""
    client.post(
        "/settings/broker",
        data={
            "broker_provider": "paper",
            "execution_mode": "paper",
            "topstep_username": " trader42 ",
            "topstep_api_key": "  abcd1234efgh5678 \n",
            "topstep_account_id": " PRACTICE-9001 ",
            "topstep_env": "demo",
            "topstep_base_url": "https://api.topstepx.com",
            "topstep_ws_url": "https://rtc.topstepx.com",
        },
        follow_redirects=False,
    )
    stored = client.app.state.settings_store.get_all_settings()
    assert stored["TOPSTEP_USERNAME"] == "trader42"
    assert stored["TOPSTEP_API_KEY"] == "abcd1234efgh5678"
    assert stored["TOPSTEP_ACCOUNT_ID"] == "PRACTICE-9001"
    s = client.app.state.settings
    assert s.topstep_username == "trader42"
    assert s.topstep_api_key == "abcd1234efgh5678"


def test_authenticate_sends_loginkey_headers_and_body(monkeypatch):
    """The exact request shape must match the ProjectX curl that worked:
    accept: text/plain, Content-Type: application/json, JSON body with
    userName + apiKey.
    """
    captured: dict[str, Any] = {}

    class _Resp:
        status_code = 200

        def json(self):
            return _login_ok("JWT.WIRE.LEVEL")

    def fake_httpx_post(url, *, json, headers, timeout):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        captured["timeout"] = timeout
        return _Resp()

    import app.execution.topstep as topstep_mod
    monkeypatch.setattr(topstep_mod.httpx, "post", fake_httpx_post)

    broker = TopstepBroker(
        username="trader42",
        api_key="abcd1234efgh5678",
        base_url="https://api.topstepx.com",
    )
    result = broker.authenticate()

    assert result["ok"] is True
    assert captured["url"] == "https://api.topstepx.com/api/Auth/loginKey"
    assert captured["json"] == {
        "userName": "trader42",
        "apiKey": "abcd1234efgh5678",
    }
    # Critically: ProjectX's loginKey wants accept: text/plain. httpx will
    # add Content-Type: application/json automatically from json=, but we
    # also set it explicitly so it matches the working curl one-for-one.
    assert captured["headers"]["accept"] == "text/plain"
    assert captured["headers"]["Content-Type"] == "application/json"


def test_authenticate_success_envelope_does_not_expose_token(monkeypatch):
    """200 + success=true + non-empty token must return ok=authenticated
    and must NOT leak the token in the returned dict."""
    def fake_post(self, path, payload, *, auth=False):
        return 200, _login_ok("JWT.SUPER.SECRET")

    monkeypatch.setattr(TopstepBroker, "_post_json", fake_post)
    broker = TopstepBroker(username="trader42", api_key="abcd1234efgh5678")
    result = broker.authenticate()
    assert result["ok"] is True
    assert result["status"] == "authenticated"
    # Token saved on the broker, but never in the envelope.
    assert broker.token == "JWT.SUPER.SECRET"
    assert "JWT.SUPER.SECRET" not in str(result)
    # Credentials summary exposes only the masked preview.
    assert result["credentials"]["token_cached"] is True
    assert "JWT.SUPER.SECRET" not in result["credentials"]["token_preview"]


def test_authenticate_failure_preserves_errorcode_3_and_does_not_call_it_wrong_credentials(
    monkeypatch,
):
    """A ProjectX errorCode=3 response must surface errorCode/errorMessage
    verbatim — we don't get to relabel it as 'wrong credentials' on our
    own."""
    def fake_post(self, path, payload, *, auth=False):
        return 200, {
            "token": "",
            "success": False,
            "errorCode": 3,
            "errorMessage": "Some ProjectX-side reason",
        }

    monkeypatch.setattr(TopstepBroker, "_post_json", fake_post)
    broker = TopstepBroker(username="trader42", api_key="abcd1234efgh5678")
    result = broker.authenticate()
    assert result["ok"] is False
    assert result["status"] == "auth_failed"
    assert result["error_code"] == 3
    assert result["error_message"] == "Some ProjectX-side reason"
    assert result["http_status"] == 200
    assert "wrong credentials" not in result["message"].lower()
    # API key must not leak into the envelope.
    assert "abcd1234efgh5678" not in str(result)


def test_authenticate_does_not_log_full_api_key_or_token(monkeypatch, caplog):
    """Debug logs must include lengths but never the secrets themselves."""
    def fake_post(self, path, payload, *, auth=False):
        return 200, _login_ok("JWT.LOG.SAFETY")

    monkeypatch.setattr(TopstepBroker, "_post_json", fake_post)
    broker = TopstepBroker(
        username="trader42",
        api_key="abcd1234efgh5678",
    )
    with caplog.at_level("INFO", logger="signalbridge.broker.topstep"):
        broker.authenticate()
    log_text = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "abcd1234efgh5678" not in log_text
    assert "JWT.LOG.SAFETY" not in log_text
    # But the lengths should be present so the operator can verify shape.
    assert "api_key_len=16" in log_text
    assert "username_len=8" in log_text
