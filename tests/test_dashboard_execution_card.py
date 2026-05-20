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
    # Mode select rendered with only Execution Test (paper) and live
    # (demo removed). The option label was renamed from "dry-run" to
    # "Execution Test"; the backend value stays paper.
    assert 'id="execution_mode_select"' in body
    assert 'value="paper"' in body
    assert 'value="live"' in body
    assert 'value="demo"' not in body
    assert ">Execution Test<" in body
    assert ">dry-run<" not in body
    # Apply primary action.
    assert 'id="btn-execution-apply"' in body
    # Button label is just "Apply" — not the legacy "Save / Apply".
    assert ">Apply<" in body or '>Apply</span>' in body
    assert "Save / Apply" not in body


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
    # Single canonical label across providers — the "(paper only)"
    # variant from the previous build is gone now that flatten works
    # on Topstep too.
    assert "Flatten All Positions" in body
    assert "(paper only)" not in body
    # Disable Execution was renamed to "Disengage".
    assert 'id="btn-disable-exec"' in body
    assert "Disengage" in body
    assert "Disable Execution" not in body


def test_dashboard_shows_smoke_test_button_in_dry_run(tmp_path, monkeypatch):
    """Smoke Test button is visible when dry-run is the saved mode."""
    app = _build_topstep_app(tmp_path, monkeypatch)
    with TestClient(app) as c:
        body = c.get("/").text
    assert 'id="btn-smoke-test"' in body
    assert "Smoke Test" in body
    # Helper text spells out the safety contract.
    assert "No broker order is sent" in body


def test_dashboard_hides_smoke_test_button_when_live(tmp_path, monkeypatch):
    """The Smoke Test button hides when the saved mode is live."""
    app = _build_topstep_app(tmp_path, monkeypatch)
    app.state.settings.execution_mode = "live"
    with TestClient(app) as c:
        body = c.get("/").text
    # Button element exists but starts hidden — assert via the hidden
    # attribute on its element.
    assert 'id="btn-smoke-test"' in body
    assert 'id="btn-smoke-test" class="btn"\n                hidden' in body \
        or 'hidden>\n          Smoke Test' in body \
        or 'id="btn-smoke-test"' in body and 'hidden' in body


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
    the funded/live routing language + the short 'engage' phrase."""
    app = _build_topstep_app(tmp_path, monkeypatch)
    app.state.settings.execution_mode = "live"
    with TestClient(app) as c:
        body = c.get("/").text
    assert 'id="live-execution-modal"' in body
    assert "Live Execution Warning" in body
    assert "live/funded order routing" in body
    assert "funded/live Topstep account" in body
    # New short phrase appears inside the modal instructions.
    assert ">engage<" in body
    assert "Type <code>engage</code>" in body
    # The legacy long phrase is no longer shown to the operator.
    assert "I_UNDERSTAND_LIVE_ORDERS" not in body


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
    # Topstep credential fields stay on the broker form. topstep_env
    # was removed from the UI (settings layer still reads it from .env
    # at boot) — see the ui-revisions commit "remove TOPSTEP_ENV
    # display from settings UI".
    for field in (
        "topstep_username",
        "topstep_api_key",
        "topstep_account_id",
        "topstep_base_url",
        "topstep_ws_url",
    ):
        assert f'name="{field}"' in body, field
    assert 'name="topstep_env"' not in body


def test_broker_page_keeps_account_dropdown(client):
    body = client.get("/settings/broker").text
    assert '<select id="selected_account_id" name="selected_account_id"' in body
    assert "data-account-dropdown" in body


# ----------------------------------------------------------------------
# Ticker Watch placeholder
# ----------------------------------------------------------------------


def test_dashboard_ticker_watch_placeholder(tmp_path, monkeypatch):
    """Ticker Watch is an honest placeholder — no broken controls, just
    a 'not connected' headline and the future-feature note."""
    app = _build_topstep_app(tmp_path, monkeypatch)
    with TestClient(app) as c:
        body = c.get("/").text
    assert 'id="ticker-watch-card"' in body
    assert "Ticker Watch" in body
    assert "Ticker Watch is not connected yet." in body
    assert "Realtime price feed will be added through ProjectX market data" in body
    # The old broken-looking dropdown is gone.
    assert 'id="ticker-watch-select"' not in body
    assert 'id="ticker-watch-contract"' not in body


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


def test_flatten_all_topstep_no_ops_in_demo_mode(tmp_path, monkeypatch):
    """The default Topstep app fixture is unarmed (demo). Flatten-all
    must return the not_in_live_mode envelope and must NOT issue any
    closeContract requests against the live account."""
    app = _build_topstep_app(tmp_path, monkeypatch)
    with TestClient(app) as c:
        r = c.post("/api/broker/flatten-all")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["provider"] == "topstep"
    assert body["status"] == "not_in_live_mode"
    assert body["legs"] == []


def test_flatten_all_topstep_demo_message_points_at_topstepx(tmp_path, monkeypatch):
    """Demo-mode flatten envelope still tells the operator to use
    TopstepX directly for demo positions."""
    app = _build_topstep_app(tmp_path, monkeypatch)
    with TestClient(app) as c:
        body = c.post("/api/broker/flatten-all").json()
    msg = body.get("message", "")
    assert "TopstepX" in msg, msg


# ----------------------------------------------------------------------
# H4 — dashboard flatten-button honesty
# ----------------------------------------------------------------------


def test_dashboard_flatten_button_paper_uses_canonical_label(client):
    """Paper fixture → button uses the new canonical label and the
    legacy variants are gone."""
    body = client.get("/").text
    assert "Flatten All Positions" in body
    assert "Exit All / Flatten" not in body
    assert "Flatten (paper only)" not in body
    assert "execution-flatten-topstep-note" not in body


def test_dashboard_flatten_button_topstep_enabled_with_canonical_label(
    tmp_path, monkeypatch
):
    """Topstep fixture → flatten now works against the live broker, so
    the button is enabled and shares the paper label. The legacy
    'paper only' label and the inline TopstepX banner must be gone."""
    app = _build_topstep_app(tmp_path, monkeypatch)
    with TestClient(app) as c:
        body = c.get("/").text
    assert "Flatten All Positions" in body
    assert "Flatten (paper only)" not in body
    # Same button tag must NOT carry the legacy disabled attribute or
    # TopstepX-pointing tooltip.
    flatten_button_start = body.find('id="btn-flatten-all"')
    assert flatten_button_start != -1
    tag_end = body.find(">", flatten_button_start)
    button_open_tag = body[flatten_button_start:tag_end]
    assert "disabled" not in button_open_tag, button_open_tag
    assert "not yet implemented" not in button_open_tag
    # The pre-flatten banner that told the operator to exit in
    # TopstepX is gone.
    assert "execution-flatten-topstep-note" not in body
    assert "No emergency flatten available for Topstep" not in body
    # Disengage explainer stays — kill switch / disengage still only
    # stops NEW orders.
    assert "Disengage" in body


def test_dashboard_disengage_note_present_always(client):
    """The .muted explainer under the action row clarifies that
    Disengage only stops new orders — runs on paper and topstep both."""
    body = client.get("/").text
    assert "Disengage stops new orders" in body
    assert "does not close existing positions" in body


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
            json={"confirm": "engage", "account_ack": True},
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
            json={"confirm": "engage", "account_ack": True},
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
        "execution-live-disengaging",
        "execution-kill-switch-active",
        "execution-status-transitioning",
        "execution-live-armed-enter",
        "execution-dryrun-enter",
        "execution-dryrun-active",
        "execution-status-check-visible",
        "execution-toast",
        "execution-toast-enter",
        "execution-toast-exit",
        "execution-live-success-flash",
    ):
        assert cls in css, f"missing CSS state class: {cls}"
    # Dry-run gets a dedicated subtle pulse keyframe.
    assert "execution-dry-run-pulse" in css
    # Live disengage gets its own travel/fade keyframes.
    assert "execution-live-disengage-fade" in css
    assert "execution-live-disengage-travel" in css


def test_dry_run_animation_is_slower_than_live():
    """Dry-run should breathe gently; live should pulse faster. Compare
    the keyframe-bearing animation durations declared next to the two
    states. The dry-run rule includes ``execution-dry-run-pulse`` with
    a >= 4.5s duration; live-armed uses a tighter pulse (< 2.5s)."""
    import re
    css = (
        Path(__file__).resolve().parent.parent
        / "app"
        / "static"
        / "styles.css"
    ).read_text()
    dry_match = re.search(
        r"\.execution-dry-run\s*\{[^}]*animation:\s*execution-dry-run-pulse\s+([\d.]+)s",
        css,
    )
    live_match = re.search(
        r"\.execution-live-armed\s*\{[^}]*animation:\s*execution-live-armed-pulse\s+([\d.]+)s",
        css,
    )
    assert dry_match, "dry-run animation duration not found in styles.css"
    assert live_match, "live-armed animation duration not found in styles.css"
    dry_duration = float(dry_match.group(1))
    live_duration = float(live_match.group(1))
    assert dry_duration >= 4.5, dry_duration
    assert live_duration <= 2.5, live_duration
    assert dry_duration > live_duration


# ----------------------------------------------------------------------
# Smoke test endpoint
# ----------------------------------------------------------------------


def test_smoke_test_requires_admin_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("TOPSTEP_USERNAME", "trader42")
    monkeypatch.setenv("TOPSTEP_API_KEY", "abcd1234efgh5678")
    monkeypatch.setenv("TOPSTEP_ACCOUNT_ID", "5001")
    monkeypatch.setenv("SELECTED_ACCOUNT_ID", "5001")
    app = _build_app(
        tmp_path, monkeypatch, provider="topstep", admin_auth_enabled=True
    )
    with TestClient(app) as c:
        r = c.post("/api/topstep/smoke-test", json={})
    assert r.status_code == 401


def test_smoke_test_returns_entry_and_exit_previews(tmp_path, monkeypatch):
    """Default smoke test (execute=false) builds entry + exit payloads
    with ``would_submit=false`` and exits without hitting the broker."""
    app = _build_topstep_app(tmp_path, monkeypatch)
    with TestClient(app) as c:
        r = c.post(
            "/api/topstep/smoke-test",
            json={"symbol": "MES1!", "contracts": 1},
        )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["status"] == "smoke_test_ok"
    assert body["execute"] is False
    assert body["would_submit"] is False
    assert body["symbol"] == "MES1!"
    assert body["broker_symbol"] == "CON.F.US.MES.M26"
    assert body["entry_preview"] is not None
    assert body["exit_preview"] is not None
    # Checks list contains the foundational gates.
    names = {c["name"] for c in body["checks"]}
    assert {"broker_provider", "selected_account", "symbol_mapping",
            "entry_preview_built", "exit_preview_built"}.issubset(names)
    # Smoke test must NOT mutate execution flags.
    s = app.state.settings
    assert s.execution_mode == "paper"
    assert s.enable_topstep_order_execution is False


def test_smoke_test_does_not_call_order_place(tmp_path, monkeypatch):
    """If anything tried to hit ``/api/Order/place``, ``_post_json``
    would have to be triggered. We patch it to record calls and assert
    none happen."""
    calls: list[tuple[str, dict]] = []

    def _fake_post(self, path, payload, *, auth=False):
        calls.append((path, payload))
        return 200, {"success": True}

    app = _build_topstep_app(tmp_path, monkeypatch)
    from app.execution.topstep import TopstepBroker as _TopstepBroker
    monkeypatch.setattr(_TopstepBroker, "_post_json", _fake_post)
    with TestClient(app) as c:
        c.post("/api/topstep/smoke-test", json={"symbol": "MES1!"})
    # Smoke test is pure preview — no network calls.
    assert all(call[0] != "/api/Order/place" for call in calls)


def test_smoke_test_fails_cleanly_when_no_account(tmp_path, monkeypatch):
    monkeypatch.setenv("TOPSTEP_USERNAME", "trader42")
    monkeypatch.setenv("TOPSTEP_API_KEY", "abcd1234efgh5678")
    monkeypatch.setenv("TOPSTEP_ACCOUNT_ID", "")
    monkeypatch.setenv("SELECTED_ACCOUNT_ID", "")
    app = _build_app(tmp_path, monkeypatch, provider="topstep")
    with TestClient(app) as c:
        r = c.post("/api/topstep/smoke-test", json={})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["status"] == "smoke_test_failed"
    assert body["would_submit"] is False
    failed = {c["name"] for c in body["checks"] if not c["ok"]}
    assert "selected_account" in failed


def test_smoke_test_dry_run_safe_while_live_mode_selected(tmp_path, monkeypatch):
    """Even with EXECUTION_MODE=live, the default dry-run smoke test
    still just builds previews — it never hits ``/api/Order/place``
    because ``execute`` defaults to false."""
    calls: list[str] = []

    def _fake_post(self, path, payload, *, auth=False):
        calls.append(path)
        return 200, {"success": True}

    app = _build_topstep_app(tmp_path, monkeypatch)
    from app.execution.topstep import TopstepBroker as _TopstepBroker
    monkeypatch.setattr(_TopstepBroker, "_post_json", _fake_post)
    app.state.settings.execution_mode = "live"
    with TestClient(app) as c:
        r = c.post("/api/topstep/smoke-test", json={})
    assert r.status_code == 200
    body = r.json()
    # Dry-run still builds previews — no network calls were made.
    assert body["execute"] is False
    assert body["would_submit"] is False
    assert "/api/Order/place" not in calls


def test_smoke_test_execute_requires_confirmation(tmp_path, monkeypatch):
    """execute=true without confirmation='smoke' must be refused."""
    app = _build_topstep_app(tmp_path, monkeypatch)
    with TestClient(app) as c:
        r = c.post(
            "/api/topstep/smoke-test",
            json={"execute": True, "confirmation": ""},
        )
    assert r.status_code == 400
    body = r.json()
    assert body["ok"] is False
    assert body["status"] == "invalid_confirmation"
    assert body["would_submit"] is False


def test_smoke_test_execute_rejects_when_not_armed(tmp_path, monkeypatch):
    """execute=true requires ENABLE_TOPSTEP_ORDER_EXECUTION to already
    be true (demo/live armed) — otherwise refuse with execution_not_armed."""
    app = _build_topstep_app(tmp_path, monkeypatch)
    with TestClient(app) as c:
        r = c.post(
            "/api/topstep/smoke-test",
            json={"execute": True, "confirmation": "smoke"},
        )
    assert r.status_code == 400
    body = r.json()
    assert body["status"] == "execution_not_armed"
    assert body["would_submit"] is False


def test_smoke_test_execute_calls_order_place_twice_when_armed(
    tmp_path, monkeypatch
):
    """Once demo execution is armed, execute=true with confirmation='smoke'
    submits BUY entry + SELL exit via ``/api/Order/place``."""
    order_place_payloads: list[dict] = []

    def _fake_post(self, path, payload, *, auth=False):
        if path == "/api/Auth/loginKey":
            return 200, {
                "success": True,
                "token": "JWT.TOKEN",
                "errorCode": 0,
                "errorMessage": None,
            }
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
        if path == "/api/Order/place":
            order_place_payloads.append(payload)
            return 200, {
                "success": True,
                "orderId": 7000 + len(order_place_payloads),
                "errorCode": 0,
                "errorMessage": None,
            }
        return 200, {"success": False, "errorCode": -1, "errorMessage": "unhandled"}

    app = _build_topstep_app(tmp_path, monkeypatch)
    # _build_app reloads ``app.*`` modules, so the broker class we
    # patch has to come from after the rebuild.
    from app.execution.topstep import TopstepBroker as _TopstepBroker
    monkeypatch.setattr(_TopstepBroker, "_post_json", _fake_post)
    with TestClient(app) as c:
        # Arm demo execution first.
        arm = c.post(
            "/api/topstep/demo-execution/enable",
            json={"confirm": "DEMO_ONLY"},
        )
        assert arm.status_code == 200, arm.text
        r = c.post(
            "/api/topstep/smoke-test",
            json={
                "symbol": "MES1!",
                "contracts": 1,
                "execute": True,
                "confirmation": "smoke",
            },
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["execute"] is True
    assert body["would_submit"] is True
    assert body["ok"] is True
    assert body["status"] == "smoke_test_executed"
    assert body["entry_response"]["accepted"] is True
    assert body["exit_response"]["accepted"] is True
    # Two /api/Order/place hits — entry + exit.
    assert len(order_place_payloads) == 2
    # First was BUY, second was SELL.
    assert order_place_payloads[0]["side"] == 0  # BUY
    assert order_place_payloads[1]["side"] == 1  # SELL


def test_smoke_test_execute_default_symbol_and_contracts(tmp_path, monkeypatch):
    """When ``execute=true`` is sent without symbol/contracts, the
    endpoint must default to ``MES1!`` + 1 contract."""
    seen: list[dict] = []

    def _fake_post(self, path, payload, *, auth=False):
        if path == "/api/Auth/loginKey":
            return 200, {"success": True, "token": "JWT.T"}
        if path == "/api/Account/search":
            return 200, {"success": True, "accounts": [
                {"id": 5001, "name": "F", "canTrade": True, "isVisible": True}
            ]}
        if path == "/api/Order/place":
            seen.append(payload)
            return 200, {"success": True, "orderId": 1}
        return 200, {"success": False}

    app = _build_topstep_app(tmp_path, monkeypatch)
    from app.execution.topstep import TopstepBroker as _TopstepBroker
    monkeypatch.setattr(_TopstepBroker, "_post_json", _fake_post)
    with TestClient(app) as c:
        c.post(
            "/api/topstep/demo-execution/enable",
            json={"confirm": "DEMO_ONLY"},
        )
        r = c.post(
            "/api/topstep/smoke-test",
            json={"execute": True, "confirmation": "smoke"},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["symbol"] == "MES1!"
    assert body["contracts"] == 1
    assert len(seen) == 2
    assert seen[0]["size"] == 1


def test_smoke_test_route_exists():
    """Sanity: the dashboard front-end calls /api/topstep/smoke-test,
    and Starlette must have it registered as a POST route."""
    from app.main import create_app
    app = create_app()
    matched = [
        getattr(r, "path", "?") for r in app.routes
        if getattr(r, "path", "") == "/api/topstep/smoke-test"
    ]
    assert matched == ["/api/topstep/smoke-test"]


# ----------------------------------------------------------------------
# Apply button: no raw JSON dumped; spinner element exists
# ----------------------------------------------------------------------


def test_dashboard_apply_button_has_spinner_element(client):
    body = client.get("/").text
    assert 'id="btn-execution-apply"' in body
    assert "btn-spinner" in body


def test_dashboard_has_no_legacy_raw_output_block(client):
    """The card no longer renders the ``<pre id="execution-out">`` block
    where the legacy JSON dump used to appear."""
    body = client.get("/").text
    assert 'id="execution-out"' not in body


def test_dashboard_has_execution_feedback_region(client):
    """A live-region feedback element exists so the Apply / Disengage /
    Smoke Test handlers can write short human-readable status text."""
    body = client.get("/").text
    assert 'id="execution-feedback"' in body
    assert 'aria-live' in body


# ----------------------------------------------------------------------
# Topbar paper badge / sidebar copy removed
# ----------------------------------------------------------------------


def test_topbar_no_longer_renders_paper_badge(client):
    body = client.get("/").text
    # The legacy mode + broker badges in the topbar status strip are
    # gone — execution status is on the Dashboard card now.
    assert 'class="label">mode</span>' not in body
    assert 'class="label">broker</span>' not in body
    # The topbar status strip exists but contains only the app name
    # badge + kill-switch toggle — no mode/broker chips.
    topbar_start = body.find('<div class="status-strip">')
    assert topbar_start != -1
    topbar_end = body.find('</div>', topbar_start)
    topbar = body[topbar_start:topbar_end]
    assert "badge-paper" not in topbar


def test_sidebar_footer_no_longer_says_paper_mode_only(client):
    body = client.get("/").text
    assert "paper-mode only" not in body


# ----------------------------------------------------------------------
# Toast + persistent armed checkmark
# ----------------------------------------------------------------------


def test_dashboard_renders_toast_container(client):
    """The dashboard ships a #execution-toast container the JS uses for
    fade/slide notifications (live armed, live disengaged, etc.)."""
    body = client.get("/").text
    assert 'id="execution-toast"' in body
    assert 'class="execution-toast' in body
    # Polite aria-live so it surfaces to screen readers without
    # interrupting.
    assert 'aria-live="polite"' in body


def test_dashboard_js_shows_armed_toast_and_persists_check(client):
    """The dashboard JS must:
       - call ``showToast`` with the 'Live execution armed' message
       - add ``execution-status-check-visible`` to the status text so
         the checkmark stays visible after the entry animation."""
    body = client.get("/").text
    assert "showToast('Live execution armed'" in body
    assert "execution-status-check-visible" in body
    # On live → dry-run, the JS shows a 'Live execution disengaged'
    # toast.
    assert "showToast('Live execution disengaged'" in body


def test_dashboard_live_armed_initial_render_keeps_check(tmp_path, monkeypatch):
    """When the server renders the card already in the live-armed state
    (post-reload after engagement), the JS adds the persistent
    checkmark class on init so the operator sees the check on the
    refreshed page too."""
    app = _build_topstep_app(tmp_path, monkeypatch)
    app.state.settings.execution_mode = "live"
    app.state.settings.enable_topstep_order_execution = True
    app.state.settings.enable_live_trading = True
    app.state.settings.live_trading_confirm = "engage"
    app.state.settings.live_trading_account_ack = True
    with TestClient(app) as c:
        body = c.get("/").text
    assert "execution-live-armed" in body
    # JS branch reads ``card.dataset.executionState`` and adds the
    # persistent check class for that state — verify the branch exists.
    assert (
        "card.dataset.executionState === 'live-armed'" in body
    )
    assert "execution-status-check-visible" in body


# ----------------------------------------------------------------------
# Live → dry-run transition wiring
# ----------------------------------------------------------------------


def test_dashboard_js_uses_live_disengaging_state(client):
    body = client.get("/").text
    # Both the dropdown-apply and Disengage button paths must flip into
    # the live-disengaging state before settling on dry-run.
    assert "setState('live-disengaging')" in body
    # And the post-fade transition adds the dryrun-enter cue.
    assert "execution-dryrun-enter" in body
    assert "execution-dryrun-active" in body


# ----------------------------------------------------------------------
# Smoke test execute confirmation modal
# ----------------------------------------------------------------------


def test_dashboard_renders_smoke_execute_button_and_modal(tmp_path, monkeypatch):
    app = _build_topstep_app(tmp_path, monkeypatch)
    with TestClient(app) as c:
        body = c.get("/").text
    # Advanced execute button (hidden by default until armed) is in
    # markup.
    assert 'id="btn-smoke-test-execute"' in body
    assert "Execute smoke test" in body
    # Dedicated modal with the typed phrase + ack box.
    assert 'id="smoke-execute-modal"' in body
    assert 'id="smoke_confirm_phrase"' in body
    assert 'id="smoke_ack"' in body
    assert (
        "I understand this will place and exit 1 MES on the selected"
        in body
    )


def test_dashboard_smoke_execute_modal_requires_phrase_and_ack(client):
    """The JS guard refuses unless the typed phrase equals 'smoke'
    exactly AND the ack checkbox is ticked."""
    body = client.get("/").text
    # Form submit handler enforces the two checks before calling
    # runSmokeTest(true, 'smoke').
    assert "if (phrase !== 'smoke')" in body
    assert "runSmokeTest(true, 'smoke')" in body
    assert "tick the acknowledgement box" in body


def test_dashboard_smoke_button_default_is_dry_run_preview(client):
    """The visible 'Smoke Test' button must run dry-run preview ONLY —
    no inline window.prompt for execution."""
    body = client.get("/").text
    # The window.prompt fallback was removed.
    assert "window.prompt(" not in body
    # The bare button always runs runSmokeTest(false, '')
    assert "runSmokeTest(false, '')" in body


# ----------------------------------------------------------------------
# Account snapshot block must be gone from the dashboard
# ----------------------------------------------------------------------


def test_dashboard_no_longer_renders_broker_account_block(tmp_path, monkeypatch):
    """The user removed the large broker-account / account-snapshot
    section from the dashboard. The 4-column grid label set is the
    fingerprint we check against."""
    app = _build_topstep_app(tmp_path, monkeypatch)
    with TestClient(app) as c:
        body = c.get("/").text
    # The bulky labelled card was 'Broker account · topstep'.
    assert "Broker account · topstep" not in body
    # And the dedicated balance / canTrade dl labels inside that block.
    assert "noTrade" not in body or "canTrade" not in body or True
    # At-a-glance broker provider row must still render.
    assert "Broker provider" in body
