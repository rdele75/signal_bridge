"""Metrics page Past Orders card tests.

Covers:
  * /metrics returns 200
  * the page contains the "Past Orders" heading
  * empty state when no orders/signals are recorded
  * paper rows are rendered from broker.get_orders()
  * Topstep rows are rendered when the mocked broker returns orders
  * Topstep "not_available" fallback when the adapter says so and the
    journal is empty
  * the page never crashes when broker.get_orders() raises or returns
    not_implemented
"""
from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from .conftest import make_alert


def test_metrics_page_returns_200(client):
    r = client.get("/metrics")
    assert r.status_code == 200
    assert "text/html" in r.headers.get("content-type", "")


def test_metrics_page_contains_past_orders_heading(client):
    r = client.get("/metrics")
    assert r.status_code == 200
    assert "Past Orders" in r.text


def test_metrics_page_renders_empty_state_when_no_orders(client):
    r = client.get("/metrics")
    assert r.status_code == 200
    # No webhook calls have been made — empty state should render.
    assert "No past orders yet" in r.text or "no past orders" in r.text.lower()


def test_metrics_page_renders_paper_orders(client):
    # Drive one accepted alert through the webhook so the paper broker
    # has at least one order to display.
    r = client.post(
        "/webhooks/tradingview",
        json=make_alert(order_id="metrics_paper_1"),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["accepted"] is True

    page = client.get("/metrics")
    assert page.status_code == 200
    html = page.text
    assert "Past Orders" in html
    # Order id and symbol from the alert should be visible in the table.
    assert "metrics_paper_1" in html
    assert "MES1!" in html


def test_metrics_page_renders_topstep_orders(make_app, monkeypatch):
    """When the Topstep adapter returns orders, the metrics page must
    render them — no crash, no fallback message."""
    monkeypatch.setenv("TOPSTEP_USERNAME", "trader42")
    monkeypatch.setenv("TOPSTEP_API_KEY", "abcd1234efgh5678")
    monkeypatch.setenv("TOPSTEP_ACCOUNT_ID", "5001")

    app = make_app(provider="topstep")
    # Import the freshly-reloaded TopstepBroker that the app actually
    # uses; the make_app factory wipes app.* from sys.modules and
    # reloads them.
    from app.execution.topstep import TopstepBroker as FreshTopstepBroker

    def fake_get_orders(self) -> dict[str, Any]:
        return {
            "ok": True,
            "provider": "topstep",
            "status": "ok",
            "orders": [
                {
                    "id": 999111,
                    "contractId": "CON.F.US.MES.M26",
                    "side": 0,
                    "size": 1,
                    "status": "Working",
                    "creationTimestamp": "2026-05-18T13:30:00Z",
                    "customTag": "topstep_order_tag",
                }
            ],
        }

    monkeypatch.setattr(FreshTopstepBroker, "get_orders", fake_get_orders)
    with TestClient(app) as c:
        r = c.get("/metrics")
    assert r.status_code == 200
    html = r.text
    assert "Past Orders" in html
    assert "999111" in html
    assert "CON.F.US.MES.M26" in html


def test_metrics_page_topstep_not_available_state(make_app, monkeypatch):
    """When Topstep get_orders comes back not_implemented AND there
    are no journal rows, the page must render the not-available
    state instead of crashing."""
    monkeypatch.setenv("TOPSTEP_USERNAME", "trader42")
    monkeypatch.setenv("TOPSTEP_API_KEY", "abcd1234efgh5678")

    app = make_app(provider="topstep")
    from app.execution.topstep import TopstepBroker as FreshTopstepBroker

    def fake_get_orders(self) -> dict[str, Any]:
        return {
            "ok": False,
            "provider": "topstep",
            "not_implemented": True,
            "status": "not_implemented",
            "orders": [],
        }

    monkeypatch.setattr(FreshTopstepBroker, "get_orders", fake_get_orders)
    with TestClient(app) as c:
        r = c.get("/metrics")
    assert r.status_code == 200
    assert "Past Orders" in r.text
    assert "Topstep order history is not available yet" in r.text


def test_metrics_page_does_not_crash_when_broker_raises(
    make_app, monkeypatch
):
    """If broker.get_orders() raises, the metrics page must still
    render (the helper traps the exception)."""
    app = make_app(provider="paper")
    from app.execution.paper import PaperBroker as FreshPaperBroker

    def boom(self) -> dict[str, Any]:
        raise RuntimeError("get_orders is angry")

    monkeypatch.setattr(FreshPaperBroker, "get_orders", boom)
    with TestClient(app) as c:
        r = c.get("/metrics")
    assert r.status_code == 200
    assert "Past Orders" in r.text


def test_api_metrics_includes_past_orders_payload(client):
    body = client.get("/api/metrics").json()
    assert "past_orders" in body
    past = body["past_orders"]
    assert isinstance(past, dict)
    assert "rows" in past
    assert isinstance(past["rows"], list)
    assert "status" in past
    assert "message" in past
