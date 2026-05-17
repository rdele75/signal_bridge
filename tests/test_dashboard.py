"""Tests for the dashboard HTML pages and /api/* endpoints."""
from __future__ import annotations

from .conftest import make_alert


# ---------- HTML pages ----------

DASHBOARD_PAGES = [
    "/",
    "/settings/broker",
    "/settings/risk",
    "/tradingview",
    "/journal",
    "/metrics",
    "/logs",
    "/system",
]


def test_all_dashboard_pages_return_200(client):
    for path in DASHBOARD_PAGES:
        r = client.get(path)
        assert r.status_code == 200, f"{path} returned {r.status_code}"
        assert "text/html" in r.headers.get("content-type", "")


def test_dashboard_renders_broker_provider(client):
    r = client.get("/")
    assert r.status_code == 200
    body = r.text
    assert "SignalBridge" in body
    assert "paper" in body  # broker provider pill


def test_tradingview_page_shows_webhook_url(client):
    r = client.get("/tradingview")
    assert r.status_code == 200
    assert "/webhooks/tradingview" in r.text


# ---------- /api/status ----------

def test_api_status(client):
    r = client.get("/api/status")
    assert r.status_code == 200
    body = r.json()
    assert body["app_name"] == "SignalBridge"
    assert body["broker_provider"] == "paper"
    assert "MES1!" in body["allowed_symbols"]


def test_api_status_broker_provider_present(client):
    body = client.get("/api/status").json()
    assert "broker_provider" in body
    assert body["broker_provider"] == "paper"


# ---------- /api/metrics ----------

def test_api_metrics_empty(client):
    body = client.get("/api/metrics").json()
    assert body["accepted_today"] == 0
    assert body["rejected_today"] == 0
    assert body["closed_total"] == 0
    assert body["win_rate"] == "N/A"


def test_api_metrics_reflects_signals(client):
    # Accept one and reject one — counts should update.
    client.post("/webhooks/tradingview", json=make_alert(order_id="m1"))
    client.post(
        "/webhooks/tradingview",
        json=make_alert(order_id="m2", symbol="AAPL"),
    )
    body = client.get("/api/metrics").json()
    assert body["accepted_today"] >= 1
    assert body["rejected_today"] >= 1
    # Rejection reasons should include the AAPL symbol_not_allowed.
    reasons = " ".join(r["reason"] for r in body["rejection_reasons"])
    assert "symbol_not_allowed" in reasons


# ---------- /api/journal/recent ----------

def test_api_journal_recent(client):
    client.post("/webhooks/tradingview", json=make_alert(order_id="j1"))
    body = client.get("/api/journal/recent?limit=5").json()
    assert "signals" in body
    assert "closed_trades" in body
    assert len(body["signals"]) >= 1


# ---------- /api/positions ----------

def test_api_positions_after_buy(client):
    client.post("/webhooks/tradingview", json=make_alert(order_id="p1"))
    body = client.get("/api/positions").json()
    assert "open_positions" in body
    assert len(body["open_positions"]) == 1
    assert body["open_positions"][0]["symbol"] == "MES1!"


# ---------- /api/kill-switch ----------

def test_api_kill_switch_toggle(client):
    on = client.post("/api/kill-switch/enable").json()
    assert on["kill_switch_active"] is True

    # New signals should now be rejected with kill_switch_active.
    r = client.post(
        "/webhooks/tradingview",
        json=make_alert(order_id="kskill_1"),
    )
    body = r.json()
    assert body["accepted"] is False
    assert body["rejection_reason"] == "kill_switch_active"

    off = client.post("/api/kill-switch/disable").json()
    assert off["kill_switch_active"] is False


# ---------- /api/broker/test-connection ----------

def test_api_broker_test_paper_success(client):
    r = client.post("/api/broker/test-connection")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["provider"] == "paper"


def test_api_broker_test_topstep_not_implemented(make_app):
    from fastapi.testclient import TestClient
    app = make_app(provider="topstep")
    with TestClient(app) as c:
        r = c.post("/api/broker/test-connection")
    assert r.status_code == 501
    body = r.json()
    assert body["ok"] is False
    assert body["provider"] == "topstep"
    assert "not implemented" in body["message"].lower()


def test_api_broker_test_tradovate_not_implemented(make_app):
    from fastapi.testclient import TestClient
    app = make_app(provider="tradovate")
    with TestClient(app) as c:
        r = c.post("/api/broker/test-connection")
    assert r.status_code == 501
    body = r.json()
    assert body["ok"] is False
    assert body["provider"] == "tradovate"
    assert "not implemented" in body["message"].lower()


# ---------- price-required behavior (already covered in test_webhook) ----------

def test_invalid_price_rejected(client):
    # Spec requires "invalid price rejected" — covered by paper broker.
    r = client.post(
        "/webhooks/tradingview",
        json=make_alert(price="", order_id="bad_price_dash"),
    )
    body = r.json()
    assert body["accepted"] is False
    assert "missing_or_invalid_price" in body["rejection_reason"]


# ---------- /system + /api/system ----------

def test_system_page_renders(client):
    r = client.get("/system")
    assert r.status_code == 200
    body = r.text
    assert "SignalBridge" in body
    assert "/webhooks/tradingview" in body
    assert "Useful local URLs" in body


def test_api_system(client):
    r = client.get("/api/system")
    assert r.status_code == 200
    body = r.json()
    assert body["app_name"] == "SignalBridge"
    assert body["broker_provider"] == "paper"
    assert body["execution_mode"] == "paper"
    assert body["webhook_path"] == "/webhooks/tradingview"
    assert body["webhook_url_local"].endswith("/webhooks/tradingview")
    # Test fixture sets a non-default secret, so it should report configured.
    assert body["webhook_secret_set"] is True
    # Storage paths come back as absolute strings.
    assert body["database_path"].endswith(".db")
    assert body["log_path"].endswith(".log")
    # Runtime status flips with kill switch.
    assert body["runtime_status"] == "running"
    assert isinstance(body["local_urls"], list) and body["local_urls"]


def test_api_system_runtime_status_halted_when_kill_switch_on(client):
    client.post("/api/kill-switch/enable")
    body = client.get("/api/system").json()
    assert body["kill_switch_active"] is True
    assert body["runtime_status"] == "halted"


# ---------- TradingView page surfaces all three URL forms ----------

def test_tradingview_page_shows_three_url_forms(client):
    body = client.get("/tradingview").text
    assert "http://127.0.0.1:" in body
    assert "http://HOST:" in body
    assert "https://YOUR-TUNNEL-URL/webhooks/tradingview" in body


# ---------- Shared layout: sidebar nav + top status strip ----------

NAV_LINKS = [
    ("Dashboard",   'href="/"'),
    ("Broker",      'href="/settings/broker"'),
    ("Risk",        'href="/settings/risk"'),
    ("TradingView", 'href="/tradingview"'),
    ("Journal",     'href="/journal"'),
    ("Metrics",     'href="/metrics"'),
    ("Logs",        'href="/logs"'),
    ("System",      'href="/system"'),
]


def test_sidebar_nav_present_on_every_page(client):
    for path in DASHBOARD_PAGES:
        body = client.get(path).text
        for label, href in NAV_LINKS:
            assert href in body, f"{path} missing nav href {href}"
            assert label in body, f"{path} missing nav label {label}"


def test_sidebar_nav_order_matches_spec(client):
    body = client.get("/").text
    positions = [body.find(href) for _, href in NAV_LINKS]
    assert all(p > 0 for p in positions), "nav hrefs missing from dashboard"
    assert positions == sorted(positions), f"nav order wrong: {positions}"


def test_top_status_bar_shows_app_name_mode_broker_killswitch(client):
    body = client.get("/").text
    assert "SignalBridge" in body
    assert "mode" in body
    assert "broker" in body
    assert "paper" in body
    assert "kill switch off" in body or "kill switch active" in body
