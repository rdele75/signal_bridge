"""Tests for the Dashboard Execution card + Ticker Watch placeholder.

Covers the UI relocation pass:

  * Execution controls are now on the Dashboard, not the Broker page.
  * The Execution card surfaces broker/mode/exec/kill-switch/account.
  * Live arming is gated by the existing confirmation phrase + ack.
  * Live mode selection animates the card border red (state class).
  * Demo arming uses an amber state class.
  * Ticker Watch is a placeholder for the future market-data hub.
  * /api/broker/flatten-all dispatches to paper or returns
    ``not_implemented`` for Topstep without ever placing live orders.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from .conftest import _build_app


def _build_topstep_app(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    selected_account: str = "5001",
):
    monkeypatch.setenv("TOPSTEP_USERNAME", "trader42")
    monkeypatch.setenv("TOPSTEP_API_KEY", "abcd1234efgh5678")
    monkeypatch.setenv("TOPSTEP_ACCOUNT_ID", selected_account)
    monkeypatch.setenv("SELECTED_ACCOUNT_ID", selected_account)
    sm_path = tmp_path / "missing_symbols.json"
    sm_path.write_text(json.dumps({"MES1!": {"topstep": "CON.F.US.MES.M26"}}))
    monkeypatch.setenv("SYMBOLS_MAP_PATH", str(sm_path))
    return _build_app(tmp_path, monkeypatch, provider="topstep")


# ----------------------------------------------------------------------
# Execution card on Dashboard
# ----------------------------------------------------------------------


def test_dashboard_renders_execution_card(client):
    """The dashboard must render an Execution card with the visible
    label and state class."""
    body = client.get("/").text
    assert 'id="exec-card"' in body
    # Title.
    assert ">Execution<" in body
    # One of the controlled state classes must be present.
    assert any(
        f"execution-{state}" in body
        for state in (
            "dry-run",
            "demo",
            "live-armed",
            "live-locked",
            "kill-switch-active",
            "disabled",
        )
    )


def test_dashboard_execution_card_shows_mode_dropdown(client):
    body = client.get("/").text
    # Mode select rendered with all three options.
    assert 'id="execution_mode_select"' in body
    assert 'value="paper"' in body
    assert 'value="demo"' in body
    assert 'value="live"' in body
    # Default fixture uses EXECUTION_MODE=paper → status pill says Dry Run.
    assert "Dry Run" in body
    # Save / Apply primary action.
    assert 'id="btn-execution-apply"' in body
    assert "Save / Apply" in body


def test_dashboard_execution_card_has_no_redundant_top_right_cluster(client):
    """The old top-right ``broker / mode / order exec / kill switch /
    account`` cluster must be gone. None of those tiny labels should
    appear inside the Execution card head row."""
    body = client.get("/").text
    # The legacy exec-meta div was the cluster wrapper.
    assert 'class="exec-meta"' not in body
    # Legacy state class prefix must be gone too.
    assert "exec-state-" not in body


def test_dashboard_execution_card_has_flatten_and_disable_buttons(client):
    body = client.get("/").text
    assert 'id="btn-flatten-all"' in body
    assert "Exit All / Flatten" in body
    assert 'id="btn-disable-exec"' in body
    assert "Disable Execution" in body


def test_dashboard_execution_card_shows_account_line(tmp_path, monkeypatch):
    """When a Topstep account is selected, the card surfaces a single
    ``Account: <id>`` line — not a meta cluster."""
    app = _build_topstep_app(tmp_path, monkeypatch, selected_account="23042921")
    with TestClient(app) as c:
        body = c.get("/").text
    assert "Account:" in body
    assert "23042921" in body


def test_dashboard_execution_card_kill_switch_active(client):
    """Kill switch active → exec card state class flips to
    ``kill-switch-active`` and the label says so."""
    client.post("/api/kill-switch/enable")
    body = client.get("/").text
    assert "execution-kill-switch-active" in body
    assert "Kill Switch Active" in body


# ----------------------------------------------------------------------
# Live + demo visual states
# ----------------------------------------------------------------------


def test_dashboard_live_mode_status_locked(tmp_path, monkeypatch):
    """When the operator picks live but hasn't engaged, the card must
    enter the ``live-locked`` state."""
    app = _build_topstep_app(tmp_path, monkeypatch)
    app.state.settings.execution_mode = "live"
    with TestClient(app) as c:
        body = c.get("/").text
    assert "execution-live-locked" in body
    assert "Live Locked" in body


def test_dashboard_demo_mode_status_demo(tmp_path, monkeypatch):
    """Demo selected → status pill says ``Demo`` and the card uses the
    ``execution-demo`` state class."""
    app = _build_topstep_app(tmp_path, monkeypatch)
    app.state.settings.execution_mode = "demo"
    with TestClient(app) as c:
        body = c.get("/").text
    assert "execution-demo" in body
    assert "Demo" in body


def test_dashboard_live_warning_modal_copy_present(tmp_path, monkeypatch):
    """The live engagement modal contains the warning headline + copy +
    the funded/live routing language."""
    app = _build_topstep_app(tmp_path, monkeypatch)
    app.state.settings.execution_mode = "live"
    with TestClient(app) as c:
        body = c.get("/").text
    assert 'id="live-execution-modal"' in body
    assert "Live Execution Warning" in body
    assert "live/funded order routing" in body
    assert "funded/live Topstep account" in body
    # Confirmation phrase must be inside the modal copy.
    assert "I_UNDERSTAND_LIVE_ORDERS" in body


# ----------------------------------------------------------------------
# Broker page relocation pointer
# ----------------------------------------------------------------------


def test_broker_page_points_execution_to_dashboard(client):
    body = client.get("/settings/broker").text
    assert "Execution controls moved to" in body
    # Link back to Dashboard.
    assert 'href="/"' in body


def test_broker_page_selection_form_has_no_execution_mode_select(client):
    """Execution mode must not be selectable from the broker form any
    more — that control belongs to the Dashboard."""
    body = client.get("/settings/broker").text
    # No visible execution-mode SELECT on the broker page.
    assert '<select id="execution_mode"' not in body
    assert 'name="execution_mode"' in body  # still posted via hidden input
    # Hidden input preserves current mode.
    assert 'type="hidden" name="execution_mode"' in body


def test_broker_page_keeps_topstep_credentials(client):
    body = client.get("/settings/broker").text
    # Topstep credential fields stay on the broker form.
    for field in (
        "topstep_username",
        "topstep_api_key",
        "topstep_account_id",
        "topstep_env",
        "topstep_base_url",
        "topstep_ws_url",
    ):
        assert f'name="{field}"' in body, field


def test_broker_page_keeps_account_dropdown(client):
    body = client.get("/settings/broker").text
    assert '<select id="selected_account_id" name="selected_account_id"' in body
    assert "data-account-dropdown" in body


# ----------------------------------------------------------------------
# Ticker Watch placeholder
# ----------------------------------------------------------------------


def test_dashboard_ticker_watch_placeholder(tmp_path, monkeypatch):
    """The dashboard must include a Ticker Watch card with a 'not
    connected yet' price + the placeholder note."""
    app = _build_topstep_app(tmp_path, monkeypatch)
    with TestClient(app) as c:
        body = c.get("/").text
    assert 'id="ticker-watch-card"' in body
    assert "Ticker Watch" in body
    assert "Not connected yet" in body
    # Mode label + future-feed note.
    assert "polling / SignalR not enabled" in body
    assert "Realtime price feed will use ProjectX market hub later." in body


# ----------------------------------------------------------------------
# /api/broker/flatten-all endpoint
# ----------------------------------------------------------------------


def test_flatten_all_paper_flattens_positions(client):
    """Paper broker → returns the existing flatten_all_positions
    envelope (ok or empty when no positions)."""
    r = client.post("/api/broker/flatten-all")
    assert r.status_code == 200
    body = r.json()
    # Paper-only contract: a successful no-op or a real flatten reply.
    assert body.get("provider") == "paper"


def test_flatten_all_topstep_returns_not_implemented(tmp_path, monkeypatch):
    """Topstep flatten-all MUST return not_implemented and not submit
    any live exit orders."""
    app = _build_topstep_app(tmp_path, monkeypatch)
    with TestClient(app) as c:
        r = c.post("/api/broker/flatten-all")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["provider"] == "topstep"
    assert body["not_implemented"] is True
    assert body["status"] == "not_implemented"


def test_flatten_all_requires_admin_when_auth_enabled(tmp_path, monkeypatch):
    """Endpoint must enforce admin auth — bare anonymous POST is 401."""
    app = _build_app(
        tmp_path, monkeypatch, provider="paper", admin_auth_enabled=True
    )
    with TestClient(app) as c:
        r = c.post("/api/broker/flatten-all")
    assert r.status_code == 401


# ----------------------------------------------------------------------
# Live execution endpoints stay protected
# ----------------------------------------------------------------------


def test_live_execution_endpoints_still_require_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("TOPSTEP_USERNAME", "trader42")
    monkeypatch.setenv("TOPSTEP_API_KEY", "abcd1234efgh5678")
    monkeypatch.setenv("TOPSTEP_ACCOUNT_ID", "5001")
    monkeypatch.setenv("SELECTED_ACCOUNT_ID", "5001")
    app = _build_app(
        tmp_path, monkeypatch, provider="topstep", admin_auth_enabled=True
    )
    with TestClient(app) as c:
        enable = c.post(
            "/api/topstep/live-execution/enable",
            json={"confirm": "I_UNDERSTAND_LIVE_ORDERS", "account_ack": True},
        )
        disable = c.post("/api/topstep/live-execution/disable")
    assert enable.status_code == 401
    assert disable.status_code == 401


def test_live_execution_not_armed_by_default(tmp_path, monkeypatch):
    """Default broker app state: live trading must remain disabled."""
    app = _build_topstep_app(tmp_path, monkeypatch)
    s = app.state.settings
    assert s.enable_live_trading is False
    assert s.execution_mode != "live"


# ----------------------------------------------------------------------
# /api/execution/apply-mode + /api/execution/disable
# ----------------------------------------------------------------------


def test_apply_mode_paper_resets_execution(tmp_path, monkeypatch):
    """Applying paper from the dashboard returns to dry-run, clears all
    execution flags, and leaves the broker connection intact."""
    app = _build_topstep_app(tmp_path, monkeypatch)
    with TestClient(app) as c:
        # Arm demo first via the existing endpoint so we can confirm
        # apply-mode=paper actually disarms it.
        c.post(
            "/api/topstep/demo-execution/enable",
            json={"confirm": "DEMO_ONLY"},
        )
        r = c.post("/api/execution/apply-mode", json={"mode": "paper"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["status"] == "execution_mode_paper"
    s = app.state.settings
    assert s.execution_mode == "paper"
    assert s.enable_topstep_order_execution is False
    assert s.topstep_execution_confirm == "disabled"
    assert s.enable_live_trading is False
    # Broker provider + selected account preserved.
    assert s.resolved_provider == "topstep"
    assert s.resolved_account_id == "5001"


def test_apply_mode_demo_auto_arms_without_phrase(tmp_path, monkeypatch):
    """Demo can be applied from the dashboard without typing DEMO_ONLY.
    The backend still sets TOPSTEP_EXECUTION_CONFIRM=DEMO_ONLY so the
    execution layer accepts demo orders."""
    app = _build_topstep_app(tmp_path, monkeypatch)
    with TestClient(app) as c:
        r = c.post("/api/execution/apply-mode", json={"mode": "demo"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["status"] == "execution_mode_demo"
    s = app.state.settings
    assert s.execution_mode == "demo"
    assert s.enable_topstep_order_execution is True
    assert s.topstep_execution_confirm == "DEMO_ONLY"
    # Live still locked.
    assert s.enable_live_trading is False


def test_apply_mode_live_is_rejected(tmp_path, monkeypatch):
    """The apply-mode endpoint refuses ``live`` — live arming uses the
    dedicated verify + enable flow."""
    app = _build_topstep_app(tmp_path, monkeypatch)
    with TestClient(app) as c:
        r = c.post("/api/execution/apply-mode", json={"mode": "live"})
    assert r.status_code == 400
    body = r.json()
    assert body["ok"] is False
    assert body["status"] == "invalid_mode"
    # Live trading must remain off.
    assert app.state.settings.enable_live_trading is False


def test_apply_mode_demo_requires_topstep_provider(tmp_path, monkeypatch):
    app = _build_app(tmp_path, monkeypatch, provider="paper")
    with TestClient(app) as c:
        r = c.post("/api/execution/apply-mode", json={"mode": "demo"})
    assert r.status_code == 400
    assert r.json()["status"] == "broker_provider_not_topstep"


def test_apply_mode_requires_admin_auth(tmp_path, monkeypatch):
    app = _build_topstep_app(tmp_path, monkeypatch)
    monkeypatch.setenv("ADMIN_AUTH_ENABLED", "true")
    app = _build_app(
        tmp_path, monkeypatch, provider="topstep", admin_auth_enabled=True
    )
    with TestClient(app) as c:
        r = c.post("/api/execution/apply-mode", json={"mode": "paper"})
    assert r.status_code == 401


def test_disable_execution_clears_every_gate(tmp_path, monkeypatch):
    """``/api/execution/disable`` returns the app to a safe dry-run
    state and disarms both demo and live flags."""
    app = _build_topstep_app(tmp_path, monkeypatch)
    with TestClient(app) as c:
        # Arm live so disable has something to flip off.
        c.post(
            "/api/topstep/live-execution/enable",
            json={"confirm": "I_UNDERSTAND_LIVE_ORDERS", "account_ack": True},
        )
        r = c.post("/api/execution/disable")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["status"] == "execution_disabled"
    s = app.state.settings
    assert s.execution_mode == "paper"
    assert s.enable_topstep_order_execution is False
    assert s.topstep_execution_confirm == "disabled"
    assert s.enable_live_trading is False
    assert s.live_trading_confirm == "disabled"
    assert s.live_trading_account_ack is False
    # Broker connection preserved.
    assert s.resolved_provider == "topstep"
    assert s.resolved_account_id == "5001"


def test_disable_execution_requires_admin_auth(tmp_path, monkeypatch):
    app = _build_app(
        tmp_path, monkeypatch, provider="paper", admin_auth_enabled=True
    )
    with TestClient(app) as c:
        r = c.post("/api/execution/disable")
    assert r.status_code == 401


# ----------------------------------------------------------------------
# Live verify endpoint (non-mutating gate preview)
# ----------------------------------------------------------------------


def test_live_verify_requires_admin_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("TOPSTEP_USERNAME", "trader42")
    monkeypatch.setenv("TOPSTEP_API_KEY", "abcd1234efgh5678")
    monkeypatch.setenv("TOPSTEP_ACCOUNT_ID", "5001")
    monkeypatch.setenv("SELECTED_ACCOUNT_ID", "5001")
    app = _build_app(
        tmp_path, monkeypatch, provider="topstep", admin_auth_enabled=True
    )
    with TestClient(app) as c:
        r = c.post("/api/topstep/live-execution/verify")
    assert r.status_code == 401


def test_live_verify_returns_ok_when_gates_pass(tmp_path, monkeypatch):
    app = _build_topstep_app(tmp_path, monkeypatch)
    with TestClient(app) as c:
        r = c.post("/api/topstep/live-execution/verify")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["failed_gates"] == []
    assert body["selected_account_id"] == "5001"
    assert body["live_max_contracts"] >= 1
    # Verify must NOT mutate settings.
    assert app.state.settings.enable_live_trading is False


def test_live_verify_returns_failed_gates(tmp_path, monkeypatch):
    """Kill switch + missing account = at least two failed gates."""
    monkeypatch.setenv("TOPSTEP_USERNAME", "trader42")
    monkeypatch.setenv("TOPSTEP_API_KEY", "abcd1234efgh5678")
    monkeypatch.setenv("TOPSTEP_ACCOUNT_ID", "")
    monkeypatch.setenv("SELECTED_ACCOUNT_ID", "")
    app = _build_app(tmp_path, monkeypatch, provider="topstep")
    app.state.kill_switch.activate("test_verify_kill")
    with TestClient(app) as c:
        r = c.post("/api/topstep/live-execution/verify")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert "no_selected_account" in body["failed_gates"]
    assert "kill_switch_active" in body["failed_gates"]
    # Did not mutate.
    assert app.state.settings.enable_live_trading is False


def test_live_verify_does_not_arm(tmp_path, monkeypatch):
    """Verify never sets live flags, even when all gates pass."""
    app = _build_topstep_app(tmp_path, monkeypatch)
    with TestClient(app) as c:
        c.post("/api/topstep/live-execution/verify")
    s = app.state.settings
    assert s.enable_live_trading is False
    assert s.execution_mode != "live"


# ----------------------------------------------------------------------
# Required CSS state classes are defined in the bundled stylesheet
# ----------------------------------------------------------------------


def test_execution_css_classes_exist():
    css = (
        Path(__file__).resolve().parent.parent
        / "app"
        / "static"
        / "styles.css"
    ).read_text()
    for cls in (
        "execution-dry-run",
        "execution-demo",
        "execution-live-locked",
        "execution-live-engaging",
        "execution-live-armed",
        "execution-kill-switch-active",
    ):
        assert cls in css, f"missing CSS state class: {cls}"
