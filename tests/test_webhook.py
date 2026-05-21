"""End-to-end webhook tests against a temp-DB FastAPI app.

Post-collapse: every test uses Topstep as the provider. Most exercise
risk-engine rejection paths (which apply uniformly across off/test/
armed) plus the off-state broker bypass. Order-submission paths are
covered in detail in test_execution.py.
"""
from __future__ import annotations

from .conftest import make_alert


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["app"] == "SignalBridge"


def test_status(client):
    r = client.get("/status")
    assert r.status_code == 200
    body = r.json()
    assert body["app_name"] == "SignalBridge"
    assert body["execution_mode"] == "off"
    assert body["broker_provider"] == "topstep"
    assert body["selected_account_id"] == "5001"
    assert "MES1!" in body["allowed_symbols"]
    assert body["kill_switch_active"] is False
    assert body["open_positions"] == []


def test_valid_alert_accepted_in_off_state(client):
    """Off state journals as accepted with no broker submission."""
    r = client.post("/webhooks/tradingview", json=make_alert())
    assert r.status_code == 200
    body = r.json()
    assert body["accepted"] is True
    assert body["decision"] == "accepted"
    assert body["execution"]["broker"] == "topstep"
    assert body["execution"]["execution_mode"] == "off"
    assert body["execution"]["message"] == "execution_off_no_submission"


def test_bad_secret_rejected(client):
    r = client.post(
        "/webhooks/tradingview",
        json=make_alert(secret="wrong", order_id="badsec"),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["accepted"] is False
    assert body["rejection_reason"] == "invalid_secret"


def test_unknown_symbol_rejected(client):
    r = client.post(
        "/webhooks/tradingview",
        json=make_alert(symbol="AAPL", order_id="badsym"),
    )
    body = r.json()
    assert body["accepted"] is False
    assert "symbol_not_allowed" in body["rejection_reason"]


def test_too_many_contracts_rejected(client):
    r = client.post(
        "/webhooks/tradingview",
        json=make_alert(contracts="99", order_id="bigsize"),
    )
    body = r.json()
    assert body["accepted"] is False
    assert "contracts_above_max" in body["rejection_reason"]


def test_duplicate_order_id_rejected(client):
    payload = make_alert(order_id="dup_xyz")
    first = client.post("/webhooks/tradingview", json=payload)
    assert first.json()["accepted"] is True

    second = client.post("/webhooks/tradingview", json=payload)
    body = second.json()
    assert body["accepted"] is False
    assert body["rejection_reason"] == "duplicate_order_id"


def test_disabled_shorts_rejected(client):
    client.app.state.settings.enable_shorts = False

    r = client.post(
        "/webhooks/tradingview",
        json=make_alert(action="sell", order_id="noshort"),
    )
    body = r.json()
    assert body["accepted"] is False
    assert body["rejection_reason"] == "shorts_disabled"


def test_missing_required_field_rejected(client):
    bad = make_alert()
    bad.pop("symbol")
    r = client.post("/webhooks/tradingview", json=bad)
    body = r.json()
    assert body["accepted"] is False
    assert "missing_required_field" in body["rejection_reason"]


def test_quoted_numeric_payload_accepted(client):
    # Classic TradingView shape: numeric fields arrive as quoted strings.
    r = client.post(
        "/webhooks/tradingview",
        json=make_alert(contracts="1", price="5000.25", order_id="quoted_ok"),
    )
    body = r.json()
    assert body["accepted"] is True
    assert body["execution"]["contracts"] == 1


def test_unquoted_numeric_payload_accepted(client):
    # Hand-rolled clients (curl, future integrations) send raw JSON numbers.
    r = client.post(
        "/webhooks/tradingview",
        json=make_alert(contracts=1, price=5000.25, order_id="unquoted_ok"),
    )
    body = r.json()
    assert body["accepted"] is True
    assert body["execution"]["contracts"] == 1


def test_non_numeric_contracts_rejected(client):
    r = client.post(
        "/webhooks/tradingview",
        json=make_alert(contracts="abc", order_id="bad_contracts"),
    )
    body = r.json()
    assert body["accepted"] is False
    assert "malformed_payload" in body["rejection_reason"]


def test_non_numeric_price_rejected(client):
    r = client.post(
        "/webhooks/tradingview",
        json=make_alert(price="not-a-price", order_id="bad_price"),
    )
    body = r.json()
    assert body["accepted"] is False
    assert "malformed_payload" in body["rejection_reason"]
