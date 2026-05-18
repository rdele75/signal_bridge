"""Admin authentication tests.

These run with ADMIN_AUTH_ENABLED=true to verify the protection contract:
  * /health and /webhooks/tradingview stay public
  * Dashboard pages redirect anonymous visitors to /login
  * Admin JSON endpoints return 401 without a session
  * Login + logout flow works end-to-end
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from .conftest import ADMIN_PASSWORD, SECRET, login_as_admin, make_alert


@pytest.fixture
def auth_client(auth_app_env):
    """Unauthenticated TestClient with auth enabled."""
    with TestClient(auth_app_env) as c:
        yield c


@pytest.fixture
def logged_in_client(auth_app_env):
    """TestClient with auth enabled AND a valid admin session."""
    with TestClient(auth_app_env) as c:
        login_as_admin(c)
        yield c


# ---------- Public endpoints stay public ----------

def test_health_works_without_login(auth_client):
    r = auth_client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"


def test_webhook_works_with_secret_without_login(auth_client):
    r = auth_client.post(
        "/webhooks/tradingview",
        json=make_alert(secret=SECRET, order_id="auth_webhook_1"),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["accepted"] is True


def test_webhook_still_rejects_bad_secret(auth_client):
    r = auth_client.post(
        "/webhooks/tradingview",
        json=make_alert(secret="wrong", order_id="auth_webhook_bad"),
    )
    body = r.json()
    assert body["accepted"] is False
    assert body["rejection_reason"] == "invalid_secret"


# ---------- Login page ----------

def test_login_page_returns_200(auth_client):
    r = auth_client.get("/login")
    assert r.status_code == 200
    assert "text/html" in r.headers.get("content-type", "")
    # Standard form fields should be present.
    body = r.text
    assert 'name="username"' in body
    assert 'name="password"' in body


def test_login_page_with_error_renders(auth_client):
    r = auth_client.get("/login?error=invalid")
    assert r.status_code == 200
    assert "Invalid" in r.text or "invalid" in r.text.lower()


# ---------- Anonymous access to protected pages redirects to /login ----------

PROTECTED_PAGES = [
    "/",
    "/settings/broker",
    "/settings/risk",
    "/tradingview",
    "/journal",
    "/metrics",
    "/logs",
    "/system",
]


@pytest.mark.parametrize("path", PROTECTED_PAGES)
def test_protected_page_redirects_to_login_when_anonymous(auth_client, path):
    r = auth_client.get(path, follow_redirects=False)
    assert r.status_code == 303, f"{path} did not redirect (got {r.status_code})"
    location = r.headers.get("location", "")
    assert location.startswith("/login"), f"{path} -> {location}"


# ---------- Anonymous access to admin APIs returns 401 ----------

PROTECTED_APIS_GET = [
    "/api/status",
    "/api/system",
    "/api/metrics",
    "/api/journal/recent",
    "/api/positions",
    "/api/broker/status",
    "/api/broker/accounts",
    "/api/broker/positions",
    "/api/broker/orders",
]


@pytest.mark.parametrize("path", PROTECTED_APIS_GET)
def test_protected_get_api_requires_auth(auth_client, path):
    r = auth_client.get(path)
    assert r.status_code == 401, f"{path} returned {r.status_code}"


def test_kill_switch_enable_requires_auth(auth_client):
    r = auth_client.post("/api/kill-switch/enable")
    assert r.status_code == 401


def test_kill_switch_disable_requires_auth(auth_client):
    r = auth_client.post("/api/kill-switch/disable")
    assert r.status_code == 401


def test_broker_test_connection_requires_auth(auth_client):
    r = auth_client.post("/api/broker/test-connection")
    assert r.status_code == 401


# ---------- Login flow ----------

def test_valid_login_allows_dashboard_access(auth_client):
    r = auth_client.post(
        "/login",
        data={"username": "admin", "password": ADMIN_PASSWORD},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers.get("location") == "/"

    # Cookie persists on the client — dashboard now loads.
    r2 = auth_client.get("/")
    assert r2.status_code == 200
    assert "SignalBridge" in r2.text


def test_wrong_password_rejected(auth_client):
    r = auth_client.post(
        "/login",
        data={"username": "admin", "password": "wrong"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    location = r.headers.get("location", "")
    assert location.startswith("/login")
    assert "error=invalid" in location

    # No session was issued, dashboard still bounces back to login.
    r2 = auth_client.get("/", follow_redirects=False)
    assert r2.status_code == 303
    assert r2.headers.get("location", "").startswith("/login")


def test_wrong_username_rejected(auth_client):
    r = auth_client.post(
        "/login",
        data={"username": "nobody", "password": ADMIN_PASSWORD},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "error=invalid" in r.headers.get("location", "")


def test_empty_credentials_rejected(auth_client):
    r = auth_client.post(
        "/login",
        data={"username": "", "password": ""},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "error=invalid" in r.headers.get("location", "")


def test_login_honors_local_next_path(auth_client):
    r = auth_client.post(
        "/login",
        data={
            "username": "admin",
            "password": ADMIN_PASSWORD,
            "next": "/metrics",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers.get("location") == "/metrics"


def test_login_ignores_external_next_path(auth_client):
    r = auth_client.post(
        "/login",
        data={
            "username": "admin",
            "password": ADMIN_PASSWORD,
            "next": "//evil.example.com/admin",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers.get("location") == "/"


# ---------- Logged-in access ----------

def test_logged_in_client_can_reach_dashboard(logged_in_client):
    r = logged_in_client.get("/")
    assert r.status_code == 200


def test_logged_in_client_sees_logout_button(logged_in_client):
    r = logged_in_client.get("/")
    assert r.status_code == 200
    assert "Sign out" in r.text
    assert 'action="/logout"' in r.text


def test_logged_in_client_can_call_api_status(logged_in_client):
    r = logged_in_client.get("/api/status")
    assert r.status_code == 200
    body = r.json()
    assert body["app_name"] == "SignalBridge"


def test_logged_in_client_can_toggle_kill_switch(logged_in_client):
    r = logged_in_client.post("/api/kill-switch/enable")
    assert r.status_code == 200
    assert r.json()["kill_switch_active"] is True


# ---------- Logout ----------

def test_logout_clears_session(logged_in_client):
    # /api/status works while logged in...
    assert logged_in_client.get("/api/status").status_code == 200

    # ... POST /logout redirects to /login and drops the admin marker.
    r = logged_in_client.post("/logout", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers.get("location") == "/login"

    # After logout the session no longer authorizes anything.
    r2 = logged_in_client.get("/api/status")
    assert r2.status_code == 401

    r3 = logged_in_client.get("/", follow_redirects=False)
    assert r3.status_code == 303
    assert r3.headers.get("location", "").startswith("/login")


def test_logout_via_get_also_clears_session(logged_in_client):
    r = logged_in_client.get("/logout", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers.get("location") == "/login"

    r2 = logged_in_client.get("/api/status")
    assert r2.status_code == 401


# ---------- Auth disabled: existing behavior preserved ----------

def test_auth_disabled_dashboard_open(client):
    """Sanity: when ADMIN_AUTH_ENABLED=false the dashboard is reachable
    without any login — keeps the existing dev-mode experience working."""
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 200


def test_auth_disabled_login_redirects_home(client):
    """When auth is off, /login is a no-op redirect to home."""
    r = client.get("/login", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers.get("location") == "/"
