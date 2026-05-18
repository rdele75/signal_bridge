"""Tests for paper-only admin actions: flatten / reset.

Covers:
  * /api/paper/flatten, /api/paper/flatten/{symbol}, /api/paper/reset
  * auth required when ADMIN_AUTH_ENABLED=true
  * flatten/reset zero open positions but never wipe the signal journal
  * topstep / tradovate provider returns a safe "not available" envelope
  * dashboard + broker pages surface the flatten / reset controls
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from .conftest import ADMIN_PASSWORD, login_as_admin, make_alert


# ---------- auth required ----------

@pytest.fixture
def auth_client(auth_app_env):
    with TestClient(auth_app_env) as c:
        yield c


@pytest.fixture
def logged_in_client(auth_app_env):
    with TestClient(auth_app_env) as c:
        login_as_admin(c)
        yield c


def test_paper_flatten_requires_auth(auth_client):
    r = auth_client.post("/api/paper/flatten")
    assert r.status_code == 401


def test_paper_flatten_symbol_requires_auth(auth_client):
    r = auth_client.post("/api/paper/flatten/MES1!")
    assert r.status_code == 401


def test_paper_reset_requires_auth(auth_client):
    r = auth_client.post("/api/paper/reset")
    assert r.status_code == 401


def test_paper_flatten_works_after_login(logged_in_client):
    # Open one paper position first.
    logged_in_client.post(
        "/webhooks/tradingview", json=make_alert(order_id="auth_flat_1")
    )
    r = logged_in_client.post("/api/paper/flatten")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["provider"] == "paper"
    assert body["event"] == "paper_flatten_all"


# ---------- functional behavior (auth disabled fixture for ergonomics) ----------

def test_flatten_all_clears_open_positions(client):
    # Open a long.
    r = client.post(
        "/webhooks/tradingview", json=make_alert(order_id="flat_open_1")
    )
    assert r.json()["accepted"] is True
    pos_before = client.get("/api/positions").json()["open_positions"]
    assert len(pos_before) == 1

    r = client.post("/api/paper/flatten")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["provider"] == "paper"
    assert body["count"] == 1
    assert body["flattened"] == ["MES1!"]
    assert body["event"] == "paper_flatten_all"

    pos_after = client.get("/api/positions").json()["open_positions"]
    assert pos_after == []


def test_flatten_no_open_positions_is_safe(client):
    body = client.post("/api/paper/flatten").json()
    assert body["ok"] is True
    assert body["count"] == 0
    assert body["flattened"] == []
    assert "no open positions" in body["message"]


def test_flatten_one_symbol_only(client):
    # Opening two symbols would normally be blocked by MAX_OPEN_POSITIONS=1,
    # so just verify the one-symbol path with one open position.
    client.post(
        "/webhooks/tradingview", json=make_alert(order_id="flatsym_1")
    )
    body = client.post("/api/paper/flatten/MES1!").json()
    assert body["ok"] is True
    assert body["symbol"] == "MES1!"
    assert body["event"] == "paper_flatten_symbol"
    assert body["flattened"] == ["MES1!"]
    assert client.get("/api/positions").json()["open_positions"] == []


def test_reset_clears_open_positions(client):
    client.post(
        "/webhooks/tradingview", json=make_alert(order_id="reset_1")
    )
    assert client.get("/api/positions").json()["open_positions"]

    body = client.post("/api/paper/reset").json()
    assert body["ok"] is True
    assert body["provider"] == "paper"
    assert body["event"] == "paper_reset_state"
    assert "MES1!" in body["cleared_symbols"]

    assert client.get("/api/positions").json()["open_positions"] == []


def test_flatten_does_not_delete_signal_journal(client):
    client.post(
        "/webhooks/tradingview", json=make_alert(order_id="journal_keep_1")
    )
    signals_before = client.get("/api/journal/recent?limit=50").json()["signals"]
    assert len(signals_before) >= 1

    client.post("/api/paper/flatten")

    signals_after = client.get("/api/journal/recent?limit=50").json()["signals"]
    assert len(signals_after) >= len(signals_before)
    ids_after = {s["id"] for s in signals_after}
    for s in signals_before:
        assert s["id"] in ids_after


def test_reset_does_not_delete_signal_journal(client):
    client.post(
        "/webhooks/tradingview", json=make_alert(order_id="journal_keep_2")
    )
    signals_before = client.get("/api/journal/recent?limit=50").json()["signals"]
    assert signals_before

    client.post("/api/paper/reset")

    signals_after = client.get("/api/journal/recent?limit=50").json()["signals"]
    ids_after = {s["id"] for s in signals_after}
    for s in signals_before:
        assert s["id"] in ids_after


# ---------- provider guard ----------

def test_flatten_safe_message_for_topstep(make_app):
    app = make_app(provider="topstep")
    with TestClient(app) as c:
        r = c.post("/api/paper/flatten")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["provider"] == "topstep"
    assert body["not_implemented"] is True
    assert "not available" in body["message"].lower()


def test_reset_safe_message_for_topstep(make_app):
    app = make_app(provider="topstep")
    with TestClient(app) as c:
        r = c.post("/api/paper/reset")
    body = r.json()
    assert r.status_code == 200
    assert body["ok"] is False
    assert body["not_implemented"] is True


def test_flatten_safe_message_for_tradovate(make_app):
    app = make_app(provider="tradovate")
    with TestClient(app) as c:
        body = c.post("/api/paper/flatten").json()
    assert body["ok"] is False
    assert body["provider"] == "tradovate"
    assert body["not_implemented"] is True


def test_flatten_symbol_safe_message_for_topstep(make_app):
    app = make_app(provider="topstep")
    with TestClient(app) as c:
        body = c.post("/api/paper/flatten/MES1!").json()
    assert body["ok"] is False
    assert body["not_implemented"] is True


# ---------- UI surfacing ----------

def test_dashboard_shows_flatten_and_reset_buttons(client):
    body = client.get("/").text
    assert "data-paper-flatten" in body
    assert "data-paper-reset" in body
    assert "Flatten All Paper Positions" in body
    assert "Reset Paper State" in body


def test_broker_page_shows_paper_controls(client):
    body = client.get("/settings/broker").text
    assert "Paper controls" in body
    assert "data-paper-flatten" in body
    assert "data-paper-reset" in body


def test_dashboard_hides_paper_controls_for_non_paper(make_app):
    """When the configured provider is topstep, the dashboard should not
    render the paper flatten/reset buttons."""
    app = make_app(provider="topstep")
    with TestClient(app) as c:
        body = c.get("/").text
    assert "data-paper-flatten" not in body
    assert "data-paper-reset" not in body
