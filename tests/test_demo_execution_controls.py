"""Tests for the Topstep demo-execution arm/disarm admin endpoints.

Exercises POST /api/topstep/demo-execution/enable and /disable. Every
test runs against a Topstep-provider app so the rules can be checked
end-to-end. Live/funded execution must never be set.

Auth coverage uses the ``auth_client`` / login pattern from
``tests/test_auth.py`` so the require-auth contract is verified
directly against the real auth dependency.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from .conftest import ADMIN_PASSWORD, _build_app, login_as_admin


def _build_topstep_app(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    admin_auth_enabled: bool = False,
    selected_account: str = "5001",
):
    """Build a Topstep-provider app with credentials + an account so the
    demo-execution preconditions are met."""
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


# ---------- Auth ----------


def test_demo_execution_enable_requires_auth(tmp_path, monkeypatch):
    app = _build_topstep_app(tmp_path, monkeypatch, admin_auth_enabled=True)
    with TestClient(app) as c:
        r = c.post(
            "/api/topstep/demo-execution/enable",
            json={"confirm": "DEMO_ONLY"},
        )
    # Admin API endpoints return 401 (not a redirect) without a session.
    assert r.status_code == 401


def test_demo_execution_disable_requires_auth(tmp_path, monkeypatch):
    app = _build_topstep_app(tmp_path, monkeypatch, admin_auth_enabled=True)
    with TestClient(app) as c:
        r = c.post("/api/topstep/demo-execution/disable")
    assert r.status_code == 401


def test_demo_execution_enable_works_with_session(tmp_path, monkeypatch):
    """End-to-end: log in then arm demo execution."""
    app = _build_topstep_app(tmp_path, monkeypatch, admin_auth_enabled=True)
    with TestClient(app) as c:
        login_as_admin(c, password=ADMIN_PASSWORD)
        r = c.post(
            "/api/topstep/demo-execution/enable",
            json={"confirm": "DEMO_ONLY"},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["status"] == "demo_execution_armed"
    assert body["enable_live_trading"] is False


# ---------- Enable: business rules ----------


def test_enable_rejects_wrong_confirmation(tmp_path, monkeypatch):
    app = _build_topstep_app(tmp_path, monkeypatch)
    with TestClient(app) as c:
        r = c.post(
            "/api/topstep/demo-execution/enable",
            json={"confirm": "demo_only"},  # wrong case
        )
    assert r.status_code == 400
    body = r.json()
    assert body["ok"] is False
    assert body["status"] == "invalid_confirmation"


def test_enable_rejects_missing_confirmation_body(tmp_path, monkeypatch):
    app = _build_topstep_app(tmp_path, monkeypatch)
    with TestClient(app) as c:
        r = c.post("/api/topstep/demo-execution/enable", json={})
    assert r.status_code == 400
    body = r.json()
    assert body["status"] == "invalid_confirmation"


def test_enable_rejects_missing_selected_account(tmp_path, monkeypatch):
    monkeypatch.setenv("TOPSTEP_USERNAME", "trader42")
    monkeypatch.setenv("TOPSTEP_API_KEY", "abcd1234efgh5678")
    monkeypatch.setenv("TOPSTEP_ACCOUNT_ID", "")
    monkeypatch.setenv("SELECTED_ACCOUNT_ID", "")
    app = _build_app(tmp_path, monkeypatch, provider="topstep")
    with TestClient(app) as c:
        r = c.post(
            "/api/topstep/demo-execution/enable",
            json={"confirm": "DEMO_ONLY"},
        )
    assert r.status_code == 400
    body = r.json()
    assert body["status"] == "no_selected_account"


def test_enable_rejects_when_provider_not_topstep(tmp_path, monkeypatch):
    app = _build_app(tmp_path, monkeypatch, provider="paper")
    with TestClient(app) as c:
        r = c.post(
            "/api/topstep/demo-execution/enable",
            json={"confirm": "DEMO_ONLY"},
        )
    assert r.status_code == 400
    body = r.json()
    assert body["status"] == "broker_provider_not_topstep"


def test_enable_sets_flags_and_never_enables_live(tmp_path, monkeypatch):
    app = _build_topstep_app(tmp_path, monkeypatch)
    with TestClient(app) as c:
        r = c.post(
            "/api/topstep/demo-execution/enable",
            json={"confirm": "DEMO_ONLY"},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["status"] == "demo_execution_armed"
    assert body["enable_topstep_order_execution"] is True
    assert body["topstep_execution_confirm"] == "DEMO_ONLY"
    assert body["execution_mode"] == "demo"
    assert body["enable_live_trading"] is False

    # Live trading must remain locked at the settings layer.
    settings = app.state.settings
    assert settings.enable_live_trading is False
    assert settings.execution_mode == "demo"
    assert settings.enable_topstep_order_execution is True
    assert settings.topstep_execution_confirm == "DEMO_ONLY"


def test_enable_rejects_when_kill_switch_active(tmp_path, monkeypatch):
    app = _build_topstep_app(tmp_path, monkeypatch)
    app.state.kill_switch.activate("test_kill_block_demo")
    with TestClient(app) as c:
        r = c.post(
            "/api/topstep/demo-execution/enable",
            json={"confirm": "DEMO_ONLY"},
        )
    assert r.status_code == 400
    body = r.json()
    assert body["status"] == "kill_switch_active"


# ---------- Disable ----------


def test_disable_clears_demo_flags(tmp_path, monkeypatch):
    app = _build_topstep_app(tmp_path, monkeypatch)
    # Arm first.
    with TestClient(app) as c:
        enable = c.post(
            "/api/topstep/demo-execution/enable",
            json={"confirm": "DEMO_ONLY"},
        )
        assert enable.status_code == 200, enable.text
        r = c.post("/api/topstep/demo-execution/disable")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["status"] == "demo_execution_disabled"
    assert body["enable_topstep_order_execution"] is False
    assert body["topstep_execution_confirm"] == "disabled"
    assert body["enable_live_trading"] is False

    settings = app.state.settings
    assert settings.enable_topstep_order_execution is False
    assert settings.topstep_execution_confirm == "disabled"
    # Broker provider and account remain untouched.
    assert settings.resolved_provider == "topstep"
    assert settings.resolved_account_id == "5001"


# ---------- Settings page surfaces the controls ----------


def test_settings_broker_page_no_longer_renders_demo_execution_section(
    tmp_path, monkeypatch
):
    """The demo arm/disarm form has moved to the Dashboard. The broker
    page should no longer host the giant ``Topstep Demo Execution``
    section; it points operators to the Dashboard instead."""
    app = _build_topstep_app(tmp_path, monkeypatch)
    with TestClient(app) as c:
        r = c.get("/settings/broker")
    assert r.status_code == 200
    html = r.text
    assert "Topstep Demo Execution" not in html
    # Pointer to dashboard.
    assert "Execution controls moved to" in html


def test_dashboard_does_not_show_demo_arm_section(tmp_path, monkeypatch):
    """The dashboard no longer renders the ``Arm demo execution`` block
    or surface the DEMO_ONLY token — selecting demo + clicking
    Save / Apply now auto-arms via the apply-mode endpoint."""
    app = _build_topstep_app(tmp_path, monkeypatch)
    with TestClient(app) as c:
        r = c.get("/")
    assert r.status_code == 200
    html = r.text
    assert "Arm demo execution" not in html
    assert "DEMO_ONLY" not in html
    # State label must reflect one of the controlled states.
    assert any(
        label in html
        for label in (
            "Dry Run",
            "Demo",
            "Live Locked",
            "Live Armed",
            "Kill Switch Active",
        )
    )


def test_dashboard_demo_mode_no_phrase_required(tmp_path, monkeypatch):
    """Saving demo from the dashboard must not require a typed phrase —
    apply-mode flips ``TOPSTEP_EXECUTION_CONFIRM`` automatically."""
    app = _build_topstep_app(tmp_path, monkeypatch)
    with TestClient(app) as c:
        # No confirm token in the body — apply-mode does it for us.
        r = c.post("/api/execution/apply-mode", json={"mode": "demo"})
    assert r.status_code == 200
    s = app.state.settings
    assert s.execution_mode == "demo"
    assert s.topstep_execution_confirm == "DEMO_ONLY"
    assert s.enable_topstep_order_execution is True
    # Live remains locked.
    assert s.enable_live_trading is False
