"""Tests for the read-only Topstep order history + realtime polling.

Covers:
  * /api/broker/order-history endpoint shape, auth, error paths
  * normalized fields (orderId, contractId, side_label, etc.)
  * /api/realtime/state snapshot
  * realtime polling defaults (disabled, polling mode)
  * polling never places orders
  * past-orders metrics UI references the new refresh + lookback wiring
  * broker page surfaces the realtime panel
  * no API key / token leaks
"""
from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from .conftest import _build_app, login_as_admin


def _build_topstep_app(
    tmp_path,
    monkeypatch,
    *,
    admin_auth_enabled: bool = False,
    selected_account: str = "5001",
):
    monkeypatch.setenv("TOPSTEP_USERNAME", "trader42")
    monkeypatch.setenv("TOPSTEP_API_KEY", "abcd1234efgh5678")
    monkeypatch.setenv("TOPSTEP_ACCOUNT_ID", selected_account)
    monkeypatch.setenv("SELECTED_ACCOUNT_ID", selected_account)
    return _build_app(
        tmp_path,
        monkeypatch,
        provider="topstep",
        admin_auth_enabled=admin_auth_enabled,
    )


def _orders_resp(*orders) -> dict[str, Any]:
    return {
        "success": True,
        "errorCode": 0,
        "errorMessage": None,
        "orders": list(orders),
    }


def _order(
    id_: int = 1234,
    contract: str = "CON.F.US.MES.M26",
    side: int = 0,
    size: int = 1,
    status: str = "Filled",
    creation: str = "2026-05-18T13:30:00Z",
    custom_tag: str = "alert_42",
) -> dict[str, Any]:
    return {
        "id": id_,
        "accountId": 5001,
        "contractId": contract,
        "creationTimestamp": creation,
        "updateTimestamp": creation,
        "status": status,
        "type": 2,
        "side": side,
        "size": size,
        "limitPrice": None,
        "stopPrice": None,
        "filledPrice": 5000.25 if status == "Filled" else None,
        "customTag": custom_tag,
    }


def _fake_post_factory(orders):
    """Mint a fake _post_json that returns ``orders`` for searches."""

    def _fake(self, path, payload, *, auth=False):
        if path == "/api/Auth/loginKey":
            return 200, {"success": True, "token": "JWT", "errorCode": 0,
                         "errorMessage": None}
        if path == "/api/Account/search":
            return 200, {
                "success": True,
                "errorCode": 0,
                "errorMessage": None,
                "accounts": [
                    {
                        "id": 5001,
                        "name": "Funded",
                        "balance": 100000.0,
                        "canTrade": True,
                        "isVisible": True,
                    }
                ],
            }
        if path == "/api/Order/search":
            return 200, _orders_resp(*orders)
        if path == "/api/Order/searchOpen":
            # Only orders with status "Working" are open — split the list.
            open_orders = [o for o in orders if o.get("status") == "Working"]
            return 200, _orders_resp(*open_orders)
        if path == "/api/Position/searchOpen":
            return 200, {
                "success": True,
                "errorCode": 0,
                "errorMessage": None,
                "positions": [],
            }
        return 200, {"success": False, "errorCode": -1, "errorMessage": "unhandled"}

    return _fake


# ----------------------------------------------------------------------
# Defaults: settings hydrate from env
# ----------------------------------------------------------------------


def test_order_history_settings_defaults(tmp_path, monkeypatch):
    app = _build_topstep_app(tmp_path, monkeypatch)
    s = app.state.settings
    assert s.order_history_lookback_days == 7
    assert s.order_history_limit == 100


def test_realtime_defaults_to_polling_disabled(tmp_path, monkeypatch):
    app = _build_topstep_app(tmp_path, monkeypatch)
    s = app.state.settings
    assert s.enable_topstep_realtime is False
    assert s.topstep_realtime_mode == "polling"
    assert s.topstep_realtime_poll_seconds == 5


# ----------------------------------------------------------------------
# /api/broker/order-history endpoint
# ----------------------------------------------------------------------


def test_order_history_requires_auth(tmp_path, monkeypatch):
    app = _build_topstep_app(tmp_path, monkeypatch, admin_auth_enabled=True)
    with TestClient(app) as c:
        r = c.get("/api/broker/order-history")
    assert r.status_code == 401


def test_order_history_returns_normalized_rows(tmp_path, monkeypatch):
    app = _build_topstep_app(tmp_path, monkeypatch)
    from app.execution.topstep import TopstepBroker as _T
    monkeypatch.setattr(
        _T, "_post_json",
        _fake_post_factory([_order(id_=999111, side=0, status="Filled"),
                            _order(id_=999112, side=1, status="Working")]),
    )
    with TestClient(app) as c:
        r = c.get("/api/broker/order-history?lookback_days=7&limit=50")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["count"] == 2
    rows = body["orders"]
    assert rows[0]["orderId"] == "999111"
    assert rows[0]["contractId"] == "CON.F.US.MES.M26"
    assert rows[0]["side_label"] == "BUY"
    assert rows[1]["side_label"] == "SELL"
    assert body["lookback_days"] == 7
    assert body["limit"] == 50


def test_order_history_uses_selected_account(tmp_path, monkeypatch):
    """Verify the adapter call body carries the configured numeric
    account id. We capture the payload by monkeypatching _post_json."""
    captured = {}

    def _capture(self, path, payload, *, auth=False):
        captured.setdefault(path, payload)
        if path == "/api/Auth/loginKey":
            return 200, {"success": True, "token": "JWT", "errorCode": 0,
                         "errorMessage": None}
        if path == "/api/Order/search":
            return 200, _orders_resp(_order(id_=1))
        return 200, {"success": True, "orders": [], "positions": [],
                     "accounts": []}

    app = _build_topstep_app(tmp_path, monkeypatch, selected_account="5001")
    from app.execution.topstep import TopstepBroker as _T
    monkeypatch.setattr(_T, "_post_json", _capture)
    with TestClient(app) as c:
        c.get("/api/broker/order-history?lookback_days=1")
    sent = captured.get("/api/Order/search")
    assert sent is not None
    assert sent["accountId"] == 5001
    assert "startTimestamp" in sent
    assert "endTimestamp" in sent


def test_order_history_handles_empty_orders(tmp_path, monkeypatch):
    app = _build_topstep_app(tmp_path, monkeypatch)
    from app.execution.topstep import TopstepBroker as _T
    monkeypatch.setattr(_T, "_post_json", _fake_post_factory([]))
    with TestClient(app) as c:
        r = c.get("/api/broker/order-history")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["orders"] == []
    assert body["count"] == 0


def test_order_history_handles_api_error(tmp_path, monkeypatch):
    """When ProjectX returns success=false, the endpoint must surface
    a structured envelope and never crash."""

    def _bad(self, path, payload, *, auth=False):
        if path == "/api/Auth/loginKey":
            return 200, {"success": True, "token": "JWT", "errorCode": 0,
                         "errorMessage": None}
        if path == "/api/Order/search":
            return 500, {
                "success": False,
                "errorCode": 5,
                "errorMessage": "internal",
                "orders": [],
            }
        return 200, {"success": True, "orders": [], "accounts": [],
                     "positions": []}

    app = _build_topstep_app(tmp_path, monkeypatch)
    from app.execution.topstep import TopstepBroker as _T
    monkeypatch.setattr(_T, "_post_json", _bad)
    with TestClient(app) as c:
        r = c.get("/api/broker/order-history")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["orders"] == []
    assert body.get("count", 0) == 0
    # The error message should be informative but no secrets.
    assert isinstance(body.get("message"), str)


def test_order_history_not_implemented_for_paper(client):
    """The paper broker has no ProjectX-style history — endpoint
    returns a clean not-implemented envelope, not a 500."""
    r = client.get("/api/broker/order-history")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["status"] == "not_implemented_for_provider"
    assert body["orders"] == []


def test_order_history_does_not_leak_secrets(tmp_path, monkeypatch):
    app = _build_topstep_app(tmp_path, monkeypatch)
    from app.execution.topstep import TopstepBroker as _T
    monkeypatch.setattr(_T, "_post_json", _fake_post_factory([_order()]))
    with TestClient(app) as c:
        r = c.get("/api/broker/order-history")
    body_text = r.text
    settings = app.state.settings
    assert settings.topstep_api_key not in body_text
    assert settings.webhook_secret not in body_text
    # Token may not exist yet — only check when set.
    if settings.topstep_token:
        assert settings.topstep_token not in body_text


# ----------------------------------------------------------------------
# /api/realtime/state endpoint
# ----------------------------------------------------------------------


def test_realtime_state_requires_auth(tmp_path, monkeypatch):
    app = _build_topstep_app(tmp_path, monkeypatch, admin_auth_enabled=True)
    with TestClient(app) as c:
        r = c.get("/api/realtime/state")
    assert r.status_code == 401


def test_realtime_state_returns_positions_and_orders(tmp_path, monkeypatch):
    app = _build_topstep_app(tmp_path, monkeypatch)
    from app.execution.topstep import TopstepBroker as _T
    monkeypatch.setattr(
        _T, "_post_json",
        _fake_post_factory([
            _order(id_=701, status="Working", side=0),
            _order(id_=702, status="Filled", side=1),
        ]),
    )
    with TestClient(app) as c:
        r = c.get("/api/realtime/state")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["realtime_mode"] == "polling"
    assert body["realtime_poll_seconds"] == 5
    assert body["realtime_enabled"] is False
    assert "refreshed_at" in body
    assert body["orders"]["ok"] is True
    # /api/Order/searchOpen filters to Working orders only in the fake.
    assert body["orders"]["count"] == 1
    assert body["positions"]["ok"] is True


def test_realtime_state_does_not_place_orders(tmp_path, monkeypatch):
    """The realtime endpoint is read-only — the only ProjectX paths it
    is allowed to call are the *search* endpoints."""
    seen_paths: list[str] = []

    def _fake(self, path, payload, *, auth=False):
        seen_paths.append(path)
        if path == "/api/Auth/loginKey":
            return 200, {"success": True, "token": "JWT", "errorCode": 0,
                         "errorMessage": None}
        if path.endswith("search") or path.endswith("searchOpen"):
            return 200, {"success": True, "orders": [], "positions": [],
                         "accounts": []}
        # Anything else (notably /api/Order/place) is forbidden.
        raise AssertionError(f"unexpected ProjectX path called: {path}")

    app = _build_topstep_app(tmp_path, monkeypatch)
    from app.execution.topstep import TopstepBroker as _T
    monkeypatch.setattr(_T, "_post_json", _fake)
    with TestClient(app) as c:
        c.get("/api/realtime/state")
    assert "/api/Order/place" not in seen_paths


# ----------------------------------------------------------------------
# Metrics Past Orders surface
# ----------------------------------------------------------------------


def test_metrics_past_orders_section_has_refresh_for_topstep(
    tmp_path, monkeypatch
):
    app = _build_topstep_app(tmp_path, monkeypatch)
    with TestClient(app) as c:
        r = c.get("/metrics")
    assert r.status_code == 200
    html = r.text
    assert "Past Orders" in html
    assert "past-orders-refresh" in html
    assert "past-orders-lookback" in html
    # Lookback options must include the three documented windows.
    assert ">1 day<" in html
    assert ">7 days<" in html
    assert ">30 days<" in html
    # /api/broker/order-history is the JS endpoint we fetch.
    assert "/api/broker/order-history" in html


def test_metrics_past_orders_empty_state_for_topstep(tmp_path, monkeypatch):
    """When Topstep has zero orders we must surface a clean empty state
    referencing the lookback window — not "No past orders yet" (which is
    the paper/journal copy)."""
    app = _build_topstep_app(tmp_path, monkeypatch)
    from app.execution.topstep import TopstepBroker as _T
    monkeypatch.setattr(_T, "_post_json", _fake_post_factory([]))
    with TestClient(app) as c:
        r = c.get("/metrics")
    assert "No Topstep orders found for this lookback window" in r.text


# ----------------------------------------------------------------------
# Broker page realtime section
# ----------------------------------------------------------------------


def test_broker_page_no_longer_renders_account_snapshot_panel(
    tmp_path, monkeypatch
):
    """The bulky Account snapshot / Realtime account data block was
    removed from /settings/broker — the user did not want a snapshot
    panel here. Backend /api/realtime/state still exists for tooling."""
    app = _build_topstep_app(tmp_path, monkeypatch)
    with TestClient(app) as c:
        r = c.get("/settings/broker")
    assert r.status_code == 200
    html = r.text
    assert "Account snapshot" not in html
    assert "Realtime account data" not in html
    # Polling controls / containers must be gone.
    assert "btn-realtime-refresh" not in html
    assert "chk-realtime-autopoll" not in html
    assert 'id="account-snapshot-panel"' not in html
    # The backend endpoint stays available for tools/tests though — make
    # sure removing the UI didn't accidentally remove the route.
    with TestClient(app) as c2:
        rt = c2.get("/api/realtime/state")
    assert rt.status_code == 200


# ----------------------------------------------------------------------
# Realtime polling helper
# ----------------------------------------------------------------------


def test_realtime_poller_refresh_returns_snapshot(tmp_path, monkeypatch):
    app = _build_topstep_app(tmp_path, monkeypatch)
    from app.execution.topstep import TopstepBroker as _T
    monkeypatch.setattr(
        _T, "_post_json",
        _fake_post_factory([_order(id_=11, status="Working")]),
    )
    from app.execution.topstep_realtime import RealtimePoller
    broker = app.state.broker
    assert isinstance(broker, _T)
    poller = RealtimePoller(broker)
    snap = poller.refresh()
    assert snap.refreshed_at is not None
    assert isinstance(snap.positions, list)
    assert isinstance(snap.orders, list)


def test_signalr_placeholder_returns_not_implemented():
    from app.execution.topstep_realtime import SignalRClientPlaceholder

    placeholder = SignalRClientPlaceholder(
        ws_url="https://rtc.topstepx.com",
        token="JWT.TOKEN",
        account_id="5001",
    )
    result = placeholder.start()
    assert result["ok"] is False
    assert result["status"] == "not_implemented"
    # The placeholder MUST NOT echo the token in its envelope.
    assert "JWT.TOKEN" not in str(result)
