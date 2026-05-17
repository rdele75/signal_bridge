"""End-to-end webhook tests against a temp-DB FastAPI app."""
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
    assert body["execution_mode"] == "paper"
    assert body["broker"] == "paper"
    assert "MES1!" in body["allowed_symbols"]
    assert body["kill_switch_active"] is False
    assert body["open_positions"] == []


def test_valid_alert_accepted(client):
    r = client.post("/webhooks/tradingview", json=make_alert())
    assert r.status_code == 200
    body = r.json()
    assert body["accepted"] is True
    assert body["decision"] == "accepted"
    assert body["execution"]["broker"] == "paper"
    assert body["execution"]["fill_price"] == 5000.25
    assert body["execution"]["position_after"]["quantity"] == 1


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


def test_disabled_shorts_rejected(client, monkeypatch, tmp_path):
    # Flip the flag on the live app's settings to simulate a restart with
    # ENABLE_SHORTS=false, without rebuilding the whole app.
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


def test_missing_price_rejected_by_broker(client):
    # Broker requires price; risk engine doesn't.
    r = client.post(
        "/webhooks/tradingview",
        json=make_alert(price="", order_id="noprice"),
    )
    body = r.json()
    assert body["accepted"] is False
    assert "missing_or_invalid_price" in body["rejection_reason"]
