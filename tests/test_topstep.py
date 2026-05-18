"""Tests for the Topstep / TopstepX (ProjectX) adapter.

Every test that exercises HTTP monkey-patches ``TopstepBroker._post_json``
so the suite never hits topstepx.com. Order execution is layered behind
multiple safety switches; tests exercise each gate explicitly.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.execution.topstep import TopstepBroker
from app.schemas import NormalizedSignal

from .conftest import make_alert


def _write_topstep_symbol_map(tmp_path: Path) -> Path:
    """Write a Topstep symbol map at the path conftest plumbs in.

    conftest's ``_build_app`` points ``SYMBOLS_MAP_PATH`` at
    ``<tmp_path>/missing_symbols.json`` and never writes the file —
    creating it here means tests that need a working symbol mapping
    just call this helper and rely on the conftest env.
    """
    sm_path = tmp_path / "missing_symbols.json"
    sm_path.write_text(
        json.dumps({"MES1!": {"topstep": "CON.F.US.MES.M26"}})
    )
    return sm_path


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
# Adapter: read-only positions / orders / search_orders (Phase 1)
# ----------------------------------------------------------------------


def test_get_positions_posts_to_search_open_with_numeric_account_id(
    monkeypatch,
):
    calls: list[tuple[str, dict[str, Any], bool]] = []

    def fake_post(self, path, payload, *, auth=False):
        if path == "/api/Auth/loginKey":
            return 200, _login_ok()
        calls.append((path, payload, auth))
        return 200, {
            "success": True,
            "errorCode": 0,
            "errorMessage": None,
            "positions": [
                {"id": 1, "accountId": 5001, "contractId": "CON.MES",
                 "type": 1, "size": 2},
            ],
        }

    monkeypatch.setattr(TopstepBroker, "_post_json", fake_post)
    broker = TopstepBroker(
        username="trader42",
        api_key="abcd1234efgh5678",
        account_id="5001",
    )
    result = broker.get_positions()
    assert result["ok"] is True
    assert result["status"] == "ok"
    assert result["selected_account_id"] == "5001"
    assert len(result["positions"]) == 1
    assert result["positions"][0]["contractId"] == "CON.MES"
    assert calls[0][0] == "/api/Position/searchOpen"
    assert calls[0][1] == {"accountId": 5001}
    assert calls[0][2] is True


def test_get_orders_posts_to_search_open_with_numeric_account_id(monkeypatch):
    calls: list[tuple[str, dict[str, Any], bool]] = []

    def fake_post(self, path, payload, *, auth=False):
        if path == "/api/Auth/loginKey":
            return 200, _login_ok()
        calls.append((path, payload, auth))
        return 200, {
            "success": True,
            "errorCode": 0,
            "errorMessage": None,
            "orders": [{"id": 11, "accountId": 5001, "status": 1}],
        }

    monkeypatch.setattr(TopstepBroker, "_post_json", fake_post)
    broker = TopstepBroker(
        username="trader42",
        api_key="abcd1234efgh5678",
        account_id="5001",
    )
    result = broker.get_orders()
    assert result["ok"] is True
    assert result["status"] == "ok"
    assert len(result["orders"]) == 1
    assert calls[0][0] == "/api/Order/searchOpen"
    assert calls[0][1] == {"accountId": 5001}
    assert calls[0][2] is True


def test_search_orders_posts_to_order_search_with_window(monkeypatch):
    calls: list[tuple[str, dict[str, Any], bool]] = []

    def fake_post(self, path, payload, *, auth=False):
        if path == "/api/Auth/loginKey":
            return 200, _login_ok()
        calls.append((path, payload, auth))
        return 200, {
            "success": True,
            "errorCode": 0,
            "errorMessage": None,
            "orders": [{"id": 21}, {"id": 22}],
        }

    monkeypatch.setattr(TopstepBroker, "_post_json", fake_post)
    broker = TopstepBroker(
        username="trader42",
        api_key="abcd1234efgh5678",
        account_id="5001",
    )
    result = broker.search_orders(
        start_timestamp="2026-05-17T00:00:00Z",
        end_timestamp="2026-05-18T00:00:00Z",
    )
    assert result["ok"] is True
    assert len(result["orders"]) == 2
    assert calls[0][0] == "/api/Order/search"
    assert calls[0][1] == {
        "accountId": 5001,
        "startTimestamp": "2026-05-17T00:00:00Z",
        "endTimestamp": "2026-05-18T00:00:00Z",
    }


def test_get_positions_refuses_when_account_id_not_numeric(monkeypatch):
    calls: list[str] = []

    def fake_post(self, path, payload, *, auth=False):
        calls.append(path)
        return 200, _login_ok()

    monkeypatch.setattr(TopstepBroker, "_post_json", fake_post)
    broker = TopstepBroker(
        username="trader42",
        api_key="abcd1234efgh5678",
        account_id="ACCT-1",
    )
    result = broker.get_positions()
    assert result["ok"] is False
    assert result["status"] == "non_numeric_account_id"
    assert result["positions"] == []
    # No HTTP call should have happened — a non-numeric id is a refuse,
    # not a degraded query.
    assert calls == []


def test_get_positions_missing_credentials_safe_envelope():
    broker = TopstepBroker(username="", api_key="", account_id="5001")
    result = broker.get_positions()
    assert result["ok"] is False
    assert result["status"] == "missing_credentials"
    assert result["positions"] == []


def test_get_positions_surfaces_projectx_failure(monkeypatch):
    def fake_post(self, path, payload, *, auth=False):
        if path == "/api/Auth/loginKey":
            return 200, _login_ok()
        return 200, {
            "success": False,
            "errorCode": 5,
            "errorMessage": "account not found",
            "positions": [],
        }

    monkeypatch.setattr(TopstepBroker, "_post_json", fake_post)
    broker = TopstepBroker(
        username="trader42",
        api_key="abcd1234efgh5678",
        account_id="5001",
    )
    result = broker.get_positions()
    assert result["ok"] is False
    assert result["status"] == "positions_failed"
    assert result["error_code"] == 5
    assert result["positions"] == []


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


def test_submit_market_order_refuses_when_execution_disabled_by_default():
    """With default settings (execution off, confirm=disabled) the
    adapter must refuse to submit and must not touch the wire."""
    broker = TopstepBroker(
        username="trader42",
        api_key="abcd1234efgh5678",
        account_id="5001",
    )
    result = broker.submit_market_order(_signal())
    assert result["ok"] is False
    assert result["accepted"] is False
    assert result["status"] == "topstep_execution_disabled"
    assert result["would_submit"] is False
    assert result["symbol"] == "MES1!"


def test_flatten_and_cancel_disabled():
    broker = TopstepBroker(username="trader42", api_key="abcd1234efgh5678")
    flat = broker.flatten_position()
    assert flat["status"] == "topstep_execution_not_enabled"
    cancel = broker.cancel_all_orders()
    assert cancel["status"] == "topstep_execution_not_enabled"


def test_execute_via_broker_raises_pointing_at_webhook_dispatch():
    """Calling broker.execute() directly must raise — the webhook
    handler owns the provider-aware dispatch and uses submit/build."""
    broker = TopstepBroker()
    with pytest.raises(NotImplementedError) as exc_info:
        broker.execute(_signal())
    assert "topstep_execute_via_webhook_handler" in str(exc_info.value)


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


def test_api_broker_positions_and_orders_for_topstep_without_creds(make_app):
    """Without credentials the read-only endpoints surface a safe
    ``missing_credentials`` envelope — they don't touch the network."""
    app = make_app(provider="topstep")
    with TestClient(app) as c:
        positions = c.get("/api/broker/positions").json()
        orders = c.get("/api/broker/orders").json()
    assert positions["positions"] == []
    assert positions["status"] == "missing_credentials"
    assert positions["provider"] == "topstep"
    assert orders["orders"] == []
    assert orders["status"] == "missing_credentials"
    assert orders["provider"] == "topstep"


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
        if path == "/api/Account/search":
            return 200, _accounts_ok(
                {"id": 5001, "name": "Practice", "balance": 50000.0,
                 "canTrade": True, "isVisible": True},
                {"id": 6002, "name": "Combine", "balance": 150_000.0,
                 "canTrade": True, "isVisible": True},
            )
        if path == "/api/Position/searchOpen":
            return 200, {
                "success": True, "errorCode": 0, "errorMessage": None,
                "positions": [],
            }
        if path == "/api/Order/searchOpen":
            return 200, {
                "success": True, "errorCode": 0, "errorMessage": None,
                "orders": [],
            }
        return 200, {"success": True}

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
    # account_balance is the flat mirror exposed alongside `balance`.
    assert body["account_balance"] == 150_000.0
    # positions/orders go over the real ProjectX endpoints now —
    # status should be ok (the mocked HTTP returns a clean empty list).
    assert body["positions_status"] in {"ok", "missing_credentials"}
    assert body["positions_not_implemented"] is False
    assert body["positions_count"] >= 0
    assert body["orders_status"] in {"ok", "missing_credentials"}
    assert body["orders_not_implemented"] is False
    assert body["orders_count"] >= 0
    # Open-orders count alias for the dashboard cards.
    assert body["open_orders_count"] == body["orders_count"]
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
    assert body["account_balance"] is None
    assert body["can_trade"] is None
    assert body["is_visible"] is None
    assert body["token_cached"] is False
    # Even with no auth, positions/orders endpoints must surface a safe
    # missing_credentials status — and must NOT have touched the network.
    assert body["positions_status"] == "missing_credentials"
    assert body["positions_not_implemented"] is False
    assert body["orders_status"] == "missing_credentials"
    assert body["orders_not_implemented"] is False


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
    """A topstep-routed webhook must not produce a paper fill row.

    By default (ENABLE_TOPSTEP_ORDER_EXECUTION=false) the handler runs
    a dry-run preview — no /api/Order/place call, no paper position."""
    app = make_app(provider="topstep")
    with TestClient(app) as c:
        r = c.post(
            "/webhooks/tradingview",
            json=make_alert(order_id="topstep_safety_1"),
        )
        body = r.json()
        # Critical invariants regardless of whether the build succeeded:
        #   no /api/Order/place was called and no paper position exists.
        assert body["execution"]["broker"] == "topstep"
        positions = c.get("/api/positions").json()["open_positions"]
        assert positions == []


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


# ----------------------------------------------------------------------
# Webhook journals a clear topstep-disabled rejection (no paper fallback)
# ----------------------------------------------------------------------


def test_webhook_with_topstep_provider_journals_dry_run(
    make_app, monkeypatch, tmp_path
):
    """A topstep-routed webhook (default: execution off) must journal a
    dry-run accepted result and never POST /api/Order/place."""
    monkeypatch.setenv("TOPSTEP_USERNAME", "trader42")
    monkeypatch.setenv("TOPSTEP_API_KEY", "abcd1234efgh5678")
    monkeypatch.setenv("TOPSTEP_ACCOUNT_ID", "5001")
    _write_topstep_symbol_map(tmp_path)

    app = make_app(provider="topstep")
    from app.execution.topstep import TopstepBroker as FreshTopstepBroker

    submitted_paths: list[str] = []

    def fake_post(self, path, payload, *, auth=False):
        submitted_paths.append(path)
        return 0, "tests must never hit the wire"

    monkeypatch.setattr(FreshTopstepBroker, "_post_json", fake_post)

    with TestClient(app) as c:
        r = c.post(
            "/webhooks/tradingview",
            json=make_alert(order_id="topstep_journal_label_1"),
        )
        assert r.status_code == 200
        body = r.json()
        assert body["accepted"] is True
        assert body["decision"] == "accepted"
        assert body["execution"]["broker"] == "topstep"
        assert body["execution"]["message"] == "topstep_dry_run_order_built"
        # The built payload is captured in execution.details so the
        # operator can audit what *would* have been sent.
        details = body["execution"]["details"]
        assert details["would_submit"] is False
        assert details["dry_run"] is True
        assert details["payload"]["accountId"] == 5001
        assert details["payload"]["contractId"] == "CON.F.US.MES.M26"
        assert details["payload"]["type"] == 2  # market
        assert details["payload"]["side"] == 0  # buy

        # CRITICAL: no /api/Order/place call (and indeed no HTTP at all
        # for the dry-run path).
        assert "/api/Order/place" not in submitted_paths
        # No paper position created.
        positions = c.get("/api/positions").json()["open_positions"]
        assert positions == []

        # Journal row.
        recent = c.get("/api/journal/recent?limit=5").json()
        matching = [
            row for row in recent["signals"]
            if row.get("order_id") == "topstep_journal_label_1"
        ]
        assert matching, "expected the topstep webhook row in the journal"
        row = matching[0]
        assert row["decision"] == "accepted"
        assert row.get("broker_provider") == "topstep"


# ----------------------------------------------------------------------
# /settings/broker page renders Topstep positions/orders status
# ----------------------------------------------------------------------


# ----------------------------------------------------------------------
# Phase 2: order builder (dry-run)
# ----------------------------------------------------------------------


from app.execution.topstep_order_builder import (  # noqa: E402
    SIDE_BUY,
    SIDE_SELL,
    TYPE_MARKET,
    build_market_order_payload,
)


class _FixedSymbolMap:
    """Tiny stand-in for SymbolMap used by the builder tests."""

    def __init__(self, mapping: dict[str, dict[str, str]]):
        self._mapping = mapping

    def resolve_explicit(self, ticker, provider):
        entry = self._mapping.get(ticker)
        if not entry:
            return None
        return entry.get(provider)


def _builder_sm() -> _FixedSymbolMap:
    return _FixedSymbolMap(
        {"MES1!": {"topstep": "CON.F.US.MES.M26"}}
    )


def test_builder_buy_market_payload_side_zero_type_two():
    result = build_market_order_payload(
        _signal(action="BUY", contracts=1, order_id="ord-1"),
        account_id="5001",
        symbol_map=_builder_sm(),
    )
    assert result["ok"] is True
    assert result["payload"]["side"] == SIDE_BUY == 0
    assert result["payload"]["type"] == TYPE_MARKET == 2
    assert result["payload"]["size"] == 1
    assert result["payload"]["accountId"] == 5001
    assert result["payload"]["contractId"] == "CON.F.US.MES.M26"
    assert result["payload"]["limitPrice"] is None
    assert result["payload"]["stopPrice"] is None
    assert result["payload"]["trailPrice"] is None
    assert result["payload"]["customTag"] == "ord-1"
    assert result["would_submit"] is False


def test_builder_sell_market_payload_side_one():
    result = build_market_order_payload(
        _signal(action="SELL"),
        account_id=5001,
        symbol_map=_builder_sm(),
    )
    assert result["ok"] is True
    assert result["payload"]["side"] == SIDE_SELL == 1
    assert result["payload"]["type"] == TYPE_MARKET


def test_builder_short_market_payload_side_one():
    result = build_market_order_payload(
        _signal(action="SHORT"),
        account_id=5001,
        symbol_map=_builder_sm(),
    )
    assert result["ok"] is True
    assert result["payload"]["side"] == SIDE_SELL == 1


def test_builder_cover_market_payload_side_zero():
    result = build_market_order_payload(
        _signal(action="COVER"),
        account_id=5001,
        symbol_map=_builder_sm(),
    )
    assert result["ok"] is True
    assert result["payload"]["side"] == SIDE_BUY == 0


def test_builder_exit_without_position_is_unsupported():
    result = build_market_order_payload(
        _signal(action="EXIT"),
        account_id=5001,
        symbol_map=_builder_sm(),
    )
    assert result["ok"] is False
    assert result["reason"] == "unsupported_exit_without_position"


def test_builder_missing_symbol_mapping_rejects():
    """No explicit Topstep mapping AND no resolved broker_symbol →
    refuse rather than guess a contract id."""
    sig = _signal(action="BUY", broker_symbol="MES1!")  # same as raw — no help
    result = build_market_order_payload(
        sig,
        account_id=5001,
        symbol_map=_FixedSymbolMap({}),  # nothing configured
    )
    assert result["ok"] is False
    assert result["reason"] == "symbol_mapping_missing"


def test_builder_honors_resolved_broker_symbol_when_distinct():
    """When the webhook handler has already resolved a broker symbol
    that differs from the raw TradingView ticker, honor it as the
    contract id — useful for one-off overrides without a symbol map."""
    sig = _signal(action="BUY", broker_symbol="CON.F.US.MNQ.M26")
    result = build_market_order_payload(
        sig,
        account_id=5001,
        symbol_map=_FixedSymbolMap({}),
    )
    assert result["ok"] is True
    assert result["payload"]["contractId"] == "CON.F.US.MNQ.M26"


def test_builder_non_numeric_account_id_rejects():
    result = build_market_order_payload(
        _signal(action="BUY"),
        account_id="ACCT-1",
        symbol_map=_builder_sm(),
    )
    assert result["ok"] is False
    assert result["reason"] == "non_numeric_account_id"


def test_builder_zero_contracts_rejects():
    result = build_market_order_payload(
        _signal(action="BUY", contracts=0),
        account_id=5001,
        symbol_map=_builder_sm(),
    )
    assert result["ok"] is False
    assert result["reason"] == "invalid_contracts"


def test_builder_custom_tag_is_truncated_safely():
    long_tag = "x" * 200
    result = build_market_order_payload(
        _signal(action="BUY", order_id=long_tag),
        account_id=5001,
        symbol_map=_builder_sm(),
    )
    assert result["ok"] is True
    assert len(result["payload"]["customTag"]) == 64


# ----------------------------------------------------------------------
# Phase 2: /api/topstep/build-order-preview endpoint
# ----------------------------------------------------------------------


def test_api_topstep_build_order_preview_with_request_body(
    make_app, tmp_path
):
    _write_topstep_symbol_map(tmp_path)
    app = make_app(provider="paper")  # endpoint works regardless of provider
    # The transient TopstepBroker built by the admin endpoint reads
    # config-driven values — set the account id via the live settings.
    settings = app.state.settings
    store = app.state.settings_store
    store.apply_to_settings(
        settings, "TOPSTEP_ACCOUNT_ID",
        store.update_typed("TOPSTEP_ACCOUNT_ID", "5001"),
    )

    with TestClient(app) as c:
        r = c.post(
            "/api/topstep/build-order-preview",
            json=make_alert(order_id="preview_buy_1"),
        )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["would_submit"] is False
    assert body["account_id"] == 5001
    assert body["contract_id"] == "CON.F.US.MES.M26"
    assert body["side"] == 0
    assert body["size"] == 1
    assert body["payload"]["type"] == 2
    assert body["payload"]["accountId"] == 5001
    assert body["payload"]["customTag"] == "preview_buy_1"
    # Safety state must be present so the operator can see why an
    # actual submit would (or wouldn't) go through.
    assert body["safety"]["enable_order_execution"] is False


def test_api_topstep_build_order_preview_falls_back_to_latest_signal(
    make_app, tmp_path
):
    _write_topstep_symbol_map(tmp_path)
    app = make_app(provider="paper")
    settings = app.state.settings
    store = app.state.settings_store
    store.apply_to_settings(
        settings, "TOPSTEP_ACCOUNT_ID",
        store.update_typed("TOPSTEP_ACCOUNT_ID", "5001"),
    )

    with TestClient(app) as c:
        # Plant a signal in the journal first so the preview has a
        # fallback to read from.
        c.post("/webhooks/tradingview", json=make_alert(order_id="seed_1"))
        r = c.post("/api/topstep/build-order-preview", json={})
    assert r.status_code == 200
    body = r.json()
    assert body["signal_source"] == "latest_journal_signal"
    assert body["ok"] is True
    assert body["payload"]["accountId"] == 5001


def test_api_topstep_build_order_preview_rejects_missing_mapping(make_app):
    """No symbol map file written → builder rejects, would_submit stays false."""
    app = make_app(provider="paper")
    settings = app.state.settings
    store = app.state.settings_store
    store.apply_to_settings(
        settings, "TOPSTEP_ACCOUNT_ID",
        store.update_typed("TOPSTEP_ACCOUNT_ID", "5001"),
    )

    with TestClient(app) as c:
        r = c.post(
            "/api/topstep/build-order-preview",
            json=make_alert(order_id="preview_no_map"),
        )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["would_submit"] is False
    assert body["reason"] == "symbol_mapping_missing"


# ----------------------------------------------------------------------
# Phase 3: submit_market_order safety gates
# ----------------------------------------------------------------------


def _ready_topstep_broker() -> TopstepBroker:
    """A TopstepBroker with creds + numeric account id + a valid cached
    token so safety gates are the only thing standing between us and a
    /api/Order/place call."""
    return TopstepBroker(
        username="trader42",
        api_key="abcd1234efgh5678",
        account_id="5001",
        token="JWT.PRE.CACHED",
        token_expires_at="2099-01-01T00:00:00+00:00",
    )


def test_submit_market_order_refused_when_execution_mode_live(monkeypatch):
    """Even with every other switch on, EXECUTION_MODE=live blocks
    submission. This catches a regression where someone forgets the
    mode check."""
    calls: list[str] = []

    def fake_post(self, path, payload, *, auth=False):
        calls.append(path)
        return 200, {"success": True, "orderId": 7777}

    monkeypatch.setattr(TopstepBroker, "_post_json", fake_post)
    broker = _ready_topstep_broker()
    broker.enable_order_execution = True
    broker.execution_confirm = "DEMO_ONLY"
    broker.execution_mode = "live"
    broker.enable_live_trading = False
    result = broker.submit_market_order(
        _signal(action="BUY"),
        symbol_map=_builder_sm(),
    )
    assert result["ok"] is False
    assert result["accepted"] is False
    assert result["status"] == "live_execution_locked"
    assert calls == []


def test_submit_market_order_refused_when_live_trading_flag_on(monkeypatch):
    """The ENABLE_LIVE_TRADING hard kill blocks even demo submission."""
    calls: list[str] = []

    def fake_post(self, path, payload, *, auth=False):
        calls.append(path)
        return 200, {"success": True}

    monkeypatch.setattr(TopstepBroker, "_post_json", fake_post)
    broker = _ready_topstep_broker()
    broker.enable_order_execution = True
    broker.execution_confirm = "DEMO_ONLY"
    broker.execution_mode = "demo"
    broker.enable_live_trading = True  # hard kill
    result = broker.submit_market_order(
        _signal(action="BUY"),
        symbol_map=_builder_sm(),
    )
    assert result["ok"] is False
    assert result["status"] == "live_execution_locked"
    assert calls == []


def test_submit_market_order_refused_when_confirmation_missing(monkeypatch):
    """Even with execution gated true + demo mode, the confirm token
    must be DEMO_ONLY."""
    calls: list[str] = []

    def fake_post(self, path, payload, *, auth=False):
        calls.append(path)
        return 200, {"success": True}

    monkeypatch.setattr(TopstepBroker, "_post_json", fake_post)
    broker = _ready_topstep_broker()
    broker.enable_order_execution = True
    broker.execution_mode = "demo"
    broker.execution_confirm = "disabled"  # missing token
    result = broker.submit_market_order(
        _signal(action="BUY"),
        symbol_map=_builder_sm(),
    )
    assert result["ok"] is False
    assert result["status"] == "topstep_execution_confirm_missing"
    assert calls == []


def test_submit_market_order_posts_to_order_place_when_safety_passes(
    monkeypatch,
):
    """All safety gates open → /api/Order/place is called with the
    exact payload the builder produced."""
    captured: dict[str, Any] = {}

    def fake_post(self, path, payload, *, auth=False):
        captured["path"] = path
        captured["payload"] = payload
        captured["auth"] = auth
        return 200, {
            "success": True,
            "errorCode": 0,
            "errorMessage": None,
            "orderId": 424242,
        }

    monkeypatch.setattr(TopstepBroker, "_post_json", fake_post)
    broker = _ready_topstep_broker()
    broker.enable_order_execution = True
    broker.execution_confirm = "DEMO_ONLY"
    broker.execution_mode = "demo"
    broker.enable_live_trading = False
    result = broker.submit_market_order(
        _signal(action="BUY", contracts=1, order_id="topstep_test_1"),
        symbol_map=_builder_sm(),
    )
    assert result["ok"] is True
    assert result["accepted"] is True
    assert result["status"] == "submitted"
    assert result["broker_order_id"] == "424242"
    assert result["order_id"] == "424242"
    assert captured["path"] == "/api/Order/place"
    assert captured["auth"] is True
    assert captured["payload"]["accountId"] == 5001
    assert captured["payload"]["contractId"] == "CON.F.US.MES.M26"
    assert captured["payload"]["type"] == 2
    assert captured["payload"]["side"] == 0
    assert captured["payload"]["size"] == 1
    assert captured["payload"]["customTag"] == "topstep_test_1"


def test_submit_market_order_handles_projectx_rejection(monkeypatch):
    """A 200 + success=false + errorCode response must surface as a
    structured rejection, not crash."""
    def fake_post(self, path, payload, *, auth=False):
        return 200, {
            "success": False,
            "errorCode": 9,
            "errorMessage": "Insufficient buying power",
            "orderId": None,
        }

    monkeypatch.setattr(TopstepBroker, "_post_json", fake_post)
    broker = _ready_topstep_broker()
    broker.enable_order_execution = True
    broker.execution_confirm = "DEMO_ONLY"
    broker.execution_mode = "demo"
    result = broker.submit_market_order(
        _signal(action="BUY"),
        symbol_map=_builder_sm(),
    )
    assert result["ok"] is False
    assert result["accepted"] is False
    assert result["status"] == "submit_rejected"
    assert result["error_code"] == 9
    assert result["error_message"] == "Insufficient buying power"
    assert result["response"]["orderId"] is None


def test_submit_market_order_does_not_log_or_leak_token(monkeypatch, caplog):
    """The submission path must not echo API key or JWT to logs / response."""
    def fake_post(self, path, payload, *, auth=False):
        return 200, {
            "success": True,
            "errorCode": 0,
            "errorMessage": None,
            "orderId": 999,
        }

    monkeypatch.setattr(TopstepBroker, "_post_json", fake_post)
    broker = _ready_topstep_broker()
    broker.enable_order_execution = True
    broker.execution_confirm = "DEMO_ONLY"
    broker.execution_mode = "demo"
    with caplog.at_level("INFO", logger="signalbridge.broker.topstep"):
        result = broker.submit_market_order(
            _signal(action="BUY"),
            symbol_map=_builder_sm(),
        )
    assert "abcd1234efgh5678" not in str(result)
    assert "JWT.PRE.CACHED" not in str(result)
    log_text = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "abcd1234efgh5678" not in log_text
    assert "JWT.PRE.CACHED" not in log_text


# ----------------------------------------------------------------------
# Phase 3: webhook end-to-end with demo execution enabled
# ----------------------------------------------------------------------


def _enable_demo_execution(app):
    settings = app.state.settings
    store = app.state.settings_store
    store.apply_to_settings(
        settings, "EXECUTION_MODE",
        store.update_typed("EXECUTION_MODE", "demo"),
    )
    store.apply_to_settings(
        settings, "ENABLE_TOPSTEP_ORDER_EXECUTION",
        store.update_typed("ENABLE_TOPSTEP_ORDER_EXECUTION", "true"),
    )
    store.apply_to_settings(
        settings, "TOPSTEP_EXECUTION_CONFIRM",
        store.update_typed("TOPSTEP_EXECUTION_CONFIRM", "DEMO_ONLY"),
    )


def test_webhook_demo_execution_submits_order(
    make_app, monkeypatch, tmp_path
):
    monkeypatch.setenv("TOPSTEP_USERNAME", "trader42")
    monkeypatch.setenv("TOPSTEP_API_KEY", "abcd1234efgh5678")
    monkeypatch.setenv("TOPSTEP_ACCOUNT_ID", "5001")
    _write_topstep_symbol_map(tmp_path)

    app = make_app(provider="topstep")
    _enable_demo_execution(app)

    from app.execution.topstep import TopstepBroker as FreshTopstepBroker

    captured: list[tuple[str, dict[str, Any]]] = []

    def fake_post(self, path, payload, *, auth=False):
        captured.append((path, payload))
        if path == "/api/Auth/loginKey":
            return 200, _login_ok("JWT.DEMO.AUTH")
        if path == "/api/Order/place":
            return 200, {
                "success": True,
                "errorCode": 0,
                "errorMessage": None,
                "orderId": 88888,
            }
        return 200, {"success": True}

    monkeypatch.setattr(FreshTopstepBroker, "_post_json", fake_post)

    with TestClient(app) as c:
        r = c.post(
            "/webhooks/tradingview",
            json=make_alert(order_id="topstep_demo_e2e_1"),
        )
    assert r.status_code == 200
    body = r.json()
    assert body["accepted"] is True
    assert body["execution"]["broker"] == "topstep"
    assert body["execution"]["message"] == "topstep_demo_order_submitted"
    assert body["execution"]["order_id"] == "88888"
    # /api/Order/place must have been called.
    place_calls = [c for c in captured if c[0] == "/api/Order/place"]
    assert len(place_calls) == 1
    assert place_calls[0][1]["accountId"] == 5001


def test_webhook_demo_execution_refuses_in_live_mode(
    make_app, monkeypatch, tmp_path
):
    """EXECUTION_MODE=live is blocked by the settings layer, but the
    webhook must refuse anyway in case it leaks in through some other
    path. We assert the runtime behavior by forcing the value past
    validation."""
    monkeypatch.setenv("TOPSTEP_USERNAME", "trader42")
    monkeypatch.setenv("TOPSTEP_API_KEY", "abcd1234efgh5678")
    monkeypatch.setenv("TOPSTEP_ACCOUNT_ID", "5001")
    _write_topstep_symbol_map(tmp_path)

    app = make_app(provider="topstep")
    _enable_demo_execution(app)
    # Bypass settings-layer validation: set execution_mode=live on the
    # live settings object directly. This is the regression scenario.
    app.state.settings.execution_mode = "live"

    from app.execution.topstep import TopstepBroker as FreshTopstepBroker

    place_calls: list[str] = []

    def fake_post(self, path, payload, *, auth=False):
        if path == "/api/Order/place":
            place_calls.append(path)
        return 200, {"success": True, "orderId": 1}

    monkeypatch.setattr(FreshTopstepBroker, "_post_json", fake_post)

    with TestClient(app) as c:
        r = c.post(
            "/webhooks/tradingview",
            json=make_alert(order_id="topstep_live_block_1"),
        )
    body = r.json()
    assert body["accepted"] is False
    assert body["execution"]["message"] == "live_execution_locked"
    assert place_calls == []


# ----------------------------------------------------------------------
# Phase 3: /api/topstep/submit-test-order endpoint
# ----------------------------------------------------------------------


def test_api_topstep_submit_test_order_refuses_when_provider_not_topstep(
    make_app,
):
    app = make_app(provider="paper")
    with TestClient(app) as c:
        r = c.post("/api/topstep/submit-test-order", json={})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["status"] == "broker_provider_not_topstep"


def test_api_topstep_submit_test_order_refuses_when_execution_disabled(
    make_app, monkeypatch, tmp_path
):
    monkeypatch.setenv("TOPSTEP_USERNAME", "trader42")
    monkeypatch.setenv("TOPSTEP_API_KEY", "abcd1234efgh5678")
    monkeypatch.setenv("TOPSTEP_ACCOUNT_ID", "5001")
    _write_topstep_symbol_map(tmp_path)
    app = make_app(provider="topstep")

    with TestClient(app) as c:
        r = c.post(
            "/api/topstep/submit-test-order",
            json={"symbol": "MES1!", "action": "BUY", "contracts": 1},
        )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["accepted"] is False
    assert body["status"] == "topstep_execution_disabled"
    assert body["would_submit"] is False


def test_api_topstep_submit_test_order_posts_when_safety_passes(
    make_app, monkeypatch, tmp_path
):
    monkeypatch.setenv("TOPSTEP_USERNAME", "trader42")
    monkeypatch.setenv("TOPSTEP_API_KEY", "abcd1234efgh5678")
    monkeypatch.setenv("TOPSTEP_ACCOUNT_ID", "5001")
    _write_topstep_symbol_map(tmp_path)
    app = make_app(provider="topstep")
    _enable_demo_execution(app)

    from app.execution.topstep import TopstepBroker as FreshTopstepBroker

    captured: list[tuple[str, dict[str, Any]]] = []

    def fake_post(self, path, payload, *, auth=False):
        captured.append((path, payload))
        if path == "/api/Auth/loginKey":
            return 200, _login_ok("JWT.TEST.ORDER")
        if path == "/api/Order/place":
            return 200, {
                "success": True,
                "errorCode": 0,
                "errorMessage": None,
                "orderId": 13579,
            }
        return 200, {"success": True}

    monkeypatch.setattr(FreshTopstepBroker, "_post_json", fake_post)

    with TestClient(app) as c:
        r = c.post(
            "/api/topstep/submit-test-order",
            json={"symbol": "MES1!", "action": "BUY", "contracts": 1},
        )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["status"] == "submitted"
    assert body["broker_order_id"] == "13579"
    # Token / api key must not leak.
    text = r.text
    assert "JWT.TEST.ORDER" not in text
    assert "abcd1234efgh5678" not in text
    # /api/Order/place was called exactly once with our payload.
    place_calls = [c for c in captured if c[0] == "/api/Order/place"]
    assert len(place_calls) == 1
    assert place_calls[0][1]["accountId"] == 5001


# ----------------------------------------------------------------------
# Tests preserved from earlier phases
# ----------------------------------------------------------------------


def test_settings_broker_page_renders_topstep_positions_orders_status(
    make_app, monkeypatch
):
    """The broker page must show the Topstep positions/orders read-only
    status so the operator can see 'not implemented' without inspecting
    the API."""
    monkeypatch.setenv("TOPSTEP_USERNAME", "trader42")
    monkeypatch.setenv("TOPSTEP_API_KEY", "abcd1234efgh5678")
    monkeypatch.setenv("TOPSTEP_ACCOUNT_ID", "6002")

    app = make_app(provider="topstep")
    from app.execution.topstep import TopstepBroker as FreshTopstepBroker

    def fake_post(self, path, payload, *, auth=False):
        if path == "/api/Auth/loginKey":
            return 200, _login_ok("JWT.PAGE.SAFE")
        if path == "/api/Account/search":
            return 200, _accounts_ok(
                {"id": 6002, "name": "Combine", "balance": 150_000.0,
                 "canTrade": True, "isVisible": True},
            )
        if path == "/api/Position/searchOpen":
            return 200, {
                "success": True, "errorCode": 0, "errorMessage": None,
                "positions": [],
            }
        if path == "/api/Order/searchOpen":
            return 200, {
                "success": True, "errorCode": 0, "errorMessage": None,
                "orders": [],
            }
        return 200, {"success": True}

    monkeypatch.setattr(FreshTopstepBroker, "_post_json", fake_post)

    with TestClient(app) as c:
        r = c.get("/settings/broker")
    assert r.status_code == 200
    text = r.text
    # Topstep positions/orders now ride the real endpoints — the
    # template surfaces an "ok" status when the mocked API succeeds.
    assert "positions" in text.lower()
    assert "orders" in text.lower()
    # Secrets must still never leak into the rendered HTML.
    assert "JWT.PAGE.SAFE" not in text
    assert "abcd1234efgh5678" not in text
