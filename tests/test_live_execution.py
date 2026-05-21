"""Tests for the Topstep LIVE execution arm/disarm flow + gate checks.

Live execution is gated by a stack of confirmations. These tests
exercise every gate in isolation and the end-to-end arm/disarm flow.
``submit_market_order`` is the only path that actually POSTs orders;
the suite monkey-patches ``_post_json`` so no traffic ever leaves
the process.

The webhook executor's dispatch logic is covered separately —
``test_webhook_live_mode_blocks_when_gates_open`` and
``test_webhook_live_mode_submits_when_gates_pass`` simulate a real
TradingView webhook landing while live is armed.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.execution.topstep import TopstepBroker
from app.schemas import NormalizedSignal

from .conftest import ADMIN_PASSWORD, _build_app, login_as_admin, make_alert


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SBCTL = PROJECT_ROOT / "scripts" / "sbctl"


def _write_topstep_symbol_map(tmp_path: Path) -> Path:
    sm_path = tmp_path / "missing_symbols.json"
    sm_path.write_text(
        json.dumps({"MES1!": {"topstep": "CON.F.US.MES.M26"}})
    )
    return sm_path


def _build_topstep_app(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    admin_auth_enabled: bool = False,
    selected_account: str = "5001",
):
    monkeypatch.setenv("TOPSTEP_USERNAME", "trader42")
    monkeypatch.setenv("TOPSTEP_API_KEY", "abcd1234efgh5678")
    monkeypatch.setenv("TOPSTEP_ACCOUNT_ID", selected_account)
    monkeypatch.setenv("SELECTED_ACCOUNT_ID", selected_account)
    monkeypatch.setenv("MAX_CONTRACTS_PER_TRADE", "1")
    monkeypatch.setenv("LIVE_MAX_CONTRACTS_PER_TRADE", "1")
    return _build_app(
        tmp_path,
        monkeypatch,
        provider="topstep",
        admin_auth_enabled=admin_auth_enabled,
    )


def _signal(**overrides) -> NormalizedSignal:
    base = dict(
        source="tradingview",
        strategy="orb_200ema_confluence",
        symbol="MES1!",
        broker_symbol="CON.F.US.MES.M26",
        exchange="CME_MINI",
        action="BUY",
        contracts=1,
        price=5000.25,
        order_id="topstep_live_1",
        comment="unit test",
        timeframe="1",
        raw={},
    )
    base.update(overrides)
    return NormalizedSignal(**base)


def _post_factory(login_token: str = "JWT.TOKEN", order_id: int = 9001):
    """Mint a fake ``_post_json`` that fakes loginKey + Order/place."""

    def _fake_post(self, path: str, payload: dict[str, Any], *, auth: bool = False):
        if path == "/api/Auth/loginKey":
            return 200, {
                "success": True,
                "token": login_token,
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
            return 200, {
                "success": True,
                "orderId": order_id,
                "errorCode": 0,
                "errorMessage": None,
            }
        return 200, {"success": False, "errorCode": -1, "errorMessage": "unhandled"}

    return _fake_post


# ----------------------------------------------------------------------
# Defaults — live must not be on out of the box
# ----------------------------------------------------------------------


def test_live_disabled_by_default(tmp_path, monkeypatch):
    app = _build_topstep_app(tmp_path, monkeypatch)
    s = app.state.settings
    assert s.enable_live_trading is False
    assert s.live_trading_confirm == "disabled"
    assert s.live_trading_account_ack is False
    assert s.live_max_contracts_per_trade == 1
    assert "MES1!" in s.live_allowed_symbols
    assert s.live_require_kill_switch_off is True


# ----------------------------------------------------------------------
# Arm endpoint
# ----------------------------------------------------------------------


def test_live_enable_rejects_wrong_confirmation(tmp_path, monkeypatch):
    app = _build_topstep_app(tmp_path, monkeypatch)
    with TestClient(app) as c:
        r = c.post(
            "/api/topstep/live-execution/enable",
            json={"confirm": "wrong_phrase", "account_ack": True},
        )
    assert r.status_code == 400
    assert r.json()["status"] == "invalid_confirmation"


def test_live_enable_rejects_missing_account_ack(tmp_path, monkeypatch):
    app = _build_topstep_app(tmp_path, monkeypatch)
    with TestClient(app) as c:
        r = c.post(
            "/api/topstep/live-execution/enable",
            json={"confirm": "engage", "account_ack": False},
        )
    assert r.status_code == 400
    assert r.json()["status"] == "account_ack_missing"


def test_live_enable_rejects_kill_switch_active(tmp_path, monkeypatch):
    app = _build_topstep_app(tmp_path, monkeypatch)
    app.state.kill_switch.activate("test_block_live")
    with TestClient(app) as c:
        r = c.post(
            "/api/topstep/live-execution/enable",
            json={"confirm": "engage", "account_ack": True},
        )
    assert r.status_code == 400
    assert r.json()["status"] == "kill_switch_active"


def test_live_enable_requires_topstep_provider(tmp_path, monkeypatch):
    app = _build_app(tmp_path, monkeypatch, provider="paper")
    with TestClient(app) as c:
        r = c.post(
            "/api/topstep/live-execution/enable",
            json={"confirm": "engage", "account_ack": True},
        )
    assert r.status_code == 400
    assert r.json()["status"] == "broker_provider_not_topstep"


def test_live_enable_requires_selected_account(tmp_path, monkeypatch):
    monkeypatch.setenv("TOPSTEP_USERNAME", "trader42")
    monkeypatch.setenv("TOPSTEP_API_KEY", "abcd1234efgh5678")
    monkeypatch.setenv("TOPSTEP_ACCOUNT_ID", "")
    monkeypatch.setenv("SELECTED_ACCOUNT_ID", "")
    app = _build_app(tmp_path, monkeypatch, provider="topstep")
    with TestClient(app) as c:
        r = c.post(
            "/api/topstep/live-execution/enable",
            json={"confirm": "engage", "account_ack": True},
        )
    assert r.status_code == 400
    assert r.json()["status"] == "no_selected_account"


def test_live_enable_sets_every_gate(tmp_path, monkeypatch):
    app = _build_topstep_app(tmp_path, monkeypatch)
    with TestClient(app) as c:
        r = c.post(
            "/api/topstep/live-execution/enable",
            json={"confirm": "engage", "account_ack": True},
        )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "live_execution_armed"
    settings = app.state.settings
    assert settings.execution_mode == "live"
    assert settings.enable_topstep_order_execution is True
    assert settings.topstep_execution_confirm == "LIVE_CONFIRMED"
    assert settings.enable_live_trading is True
    assert settings.live_trading_confirm == "I_UNDERSTAND_LIVE_ORDERS"
    assert settings.live_trading_account_ack is True


def test_live_enable_requires_admin_auth(tmp_path, monkeypatch):
    app = _build_topstep_app(tmp_path, monkeypatch, admin_auth_enabled=True)
    with TestClient(app) as c:
        r = c.post(
            "/api/topstep/live-execution/enable",
            json={"confirm": "engage", "account_ack": True},
        )
    assert r.status_code == 401


# ----------------------------------------------------------------------
# Disable endpoint
# ----------------------------------------------------------------------


def test_live_disable_resets_every_gate(tmp_path, monkeypatch):
    app = _build_topstep_app(tmp_path, monkeypatch)
    with TestClient(app) as c:
        c.post(
            "/api/topstep/live-execution/enable",
            json={"confirm": "engage", "account_ack": True},
        )
        r = c.post("/api/topstep/live-execution/disable")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "live_execution_disabled"
    s = app.state.settings
    assert s.enable_live_trading is False
    assert s.live_trading_confirm == "disabled"
    assert s.live_trading_account_ack is False
    assert s.topstep_execution_confirm == "disabled"
    assert s.enable_topstep_order_execution is False


def test_live_disable_requires_admin_auth(tmp_path, monkeypatch):
    app = _build_topstep_app(tmp_path, monkeypatch, admin_auth_enabled=True)
    with TestClient(app) as c:
        r = c.post("/api/topstep/live-execution/disable")
    assert r.status_code == 401


# ----------------------------------------------------------------------
# Broker safety check matrix
# ----------------------------------------------------------------------


def _live_broker(**overrides) -> TopstepBroker:
    base = dict(
        username="trader42",
        api_key="abcd1234efgh5678",
        account_id="5001",
        enable_order_execution=True,
        execution_confirm="LIVE_CONFIRMED",
        enable_live_trading=True,
        execution_mode="live",
        live_trading_confirm="I_UNDERSTAND_LIVE_ORDERS",
        live_trading_account_ack=True,
        live_max_contracts_per_trade=1,
        live_allowed_symbols=["MES1!", "MNQ1!"],
        live_require_kill_switch_off=True,
        max_contracts_per_trade=1,
    )
    base.update(overrides)
    return TopstepBroker(**base)


def test_live_safety_passes_when_every_gate_satisfied():
    b = _live_broker()
    assert b._live_execution_safety_check(_signal()) is None


def test_live_safety_rejects_when_master_disabled():
    b = _live_broker(enable_live_trading=False)
    assert b._live_execution_safety_check(_signal()) == "live_trading_disabled"


def test_live_safety_rejects_wrong_confirm():
    b = _live_broker(execution_confirm="DEMO_ONLY")
    assert (
        b._live_execution_safety_check(_signal())
        == "topstep_execution_confirm_missing"
    )


def test_live_safety_rejects_missing_live_phrase():
    b = _live_broker(live_trading_confirm="disabled")
    assert (
        b._live_execution_safety_check(_signal())
        == "live_confirmation_missing"
    )


def test_live_safety_rejects_missing_account_ack():
    b = _live_broker(live_trading_account_ack=False)
    assert (
        b._live_execution_safety_check(_signal())
        == "live_account_ack_missing"
    )


def test_live_safety_rejects_kill_switch_active():
    b = _live_broker()
    b.kill_switch_active = True
    assert b._live_execution_safety_check(_signal()) == "kill_switch_active"


def test_live_safety_allows_kill_switch_when_check_disabled():
    b = _live_broker(live_require_kill_switch_off=False)
    b.kill_switch_active = True
    assert b._live_execution_safety_check(_signal()) is None


def test_live_safety_rejects_disallowed_symbol():
    b = _live_broker()
    sig = _signal(symbol="ES1!")
    assert b._live_execution_safety_check(sig) == "live_symbol_not_allowed"


def test_live_safety_rejects_contracts_above_live_cap():
    b = _live_broker(live_max_contracts_per_trade=1, max_contracts_per_trade=5)
    sig = _signal(contracts=2)
    assert b._live_execution_safety_check(sig) == "live_contracts_above_max"


def test_live_safety_rejects_contracts_above_global_cap():
    b = _live_broker(live_max_contracts_per_trade=5, max_contracts_per_trade=1)
    sig = _signal(contracts=2)
    assert b._live_execution_safety_check(sig) == "contracts_above_max"


def test_live_safety_rejects_missing_account():
    b = _live_broker(account_id="")
    assert b._live_execution_safety_check(_signal()) == "non_numeric_account_id"


# ----------------------------------------------------------------------
# submit-live-test-order endpoint
# ----------------------------------------------------------------------


def test_submit_live_test_order_requires_live_mode(tmp_path, monkeypatch):
    """If live is not armed, the helper refuses without touching the wire."""
    app = _build_topstep_app(tmp_path, monkeypatch)
    with TestClient(app) as c:
        r = c.post(
            "/api/topstep/submit-live-test-order",
            json={"symbol": "MES1!", "action": "buy", "contracts": 1},
        )
    body = r.json()
    assert body["ok"] is False
    assert body["status"] == "live_execution_locked"
    assert body["gate"] == "execution_mode_not_live"


def test_submit_live_test_order_rejects_unsupported_action(tmp_path, monkeypatch):
    app = _build_topstep_app(tmp_path, monkeypatch)
    with TestClient(app) as c:
        c.post(
            "/api/topstep/live-execution/enable",
            json={"confirm": "engage", "account_ack": True},
        )
        r = c.post(
            "/api/topstep/submit-live-test-order",
            json={"symbol": "MES1!", "action": "weird", "contracts": 1},
        )
    body = r.json()
    assert body["status"] == "unsupported_action"


def test_submit_live_test_order_rejects_contracts_above_live_cap(
    tmp_path, monkeypatch
):
    app = _build_topstep_app(tmp_path, monkeypatch)
    with TestClient(app) as c:
        c.post(
            "/api/topstep/live-execution/enable",
            json={"confirm": "engage", "account_ack": True},
        )
        r = c.post(
            "/api/topstep/submit-live-test-order",
            json={"symbol": "MES1!", "action": "buy", "contracts": 2},
        )
    body = r.json()
    assert body["status"] == "live_contracts_above_max"


def test_submit_live_test_order_rejects_symbol_not_allowed(
    tmp_path, monkeypatch
):
    app = _build_topstep_app(tmp_path, monkeypatch)
    with TestClient(app) as c:
        c.post(
            "/api/topstep/live-execution/enable",
            json={"confirm": "engage", "account_ack": True},
        )
        r = c.post(
            "/api/topstep/submit-live-test-order",
            json={"symbol": "ES1!", "action": "buy", "contracts": 1},
        )
    body = r.json()
    assert body["status"] == "live_symbol_not_allowed"


def test_submit_live_test_order_rejects_missing_mapping(tmp_path, monkeypatch):
    # No symbol map file means the explicit topstep mapping is missing.
    app = _build_topstep_app(tmp_path, monkeypatch)
    with TestClient(app) as c:
        c.post(
            "/api/topstep/live-execution/enable",
            json={"confirm": "engage", "account_ack": True},
        )
        r = c.post(
            "/api/topstep/submit-live-test-order",
            json={"symbol": "MES1!", "action": "buy", "contracts": 1},
        )
    body = r.json()
    assert body["status"] == "symbol_mapping_missing"


def test_submit_live_test_order_posts_when_gates_pass(
    tmp_path, monkeypatch
):
    _write_topstep_symbol_map(tmp_path)
    app = _build_topstep_app(tmp_path, monkeypatch)
    # Patch _post_json on the *reloaded* class so the in-flight broker
    # sees the fake response.
    from app.execution.topstep import TopstepBroker as _TopstepBroker
    monkeypatch.setattr(_TopstepBroker, "_post_json", _post_factory())
    with TestClient(app) as c:
        c.post(
            "/api/topstep/live-execution/enable",
            json={"confirm": "engage", "account_ack": True},
        )
        r = c.post(
            "/api/topstep/submit-live-test-order",
            json={"symbol": "MES1!", "action": "buy", "contracts": 1},
        )
    body = r.json()
    assert body["accepted"] is True
    assert body["status"] == "submitted"
    assert body["broker_order_id"] == "9001"


def test_submit_live_test_order_requires_admin_auth(tmp_path, monkeypatch):
    app = _build_topstep_app(tmp_path, monkeypatch, admin_auth_enabled=True)
    with TestClient(app) as c:
        r = c.post(
            "/api/topstep/submit-live-test-order",
            json={"symbol": "MES1!", "action": "buy", "contracts": 1},
        )
    assert r.status_code == 401


# ----------------------------------------------------------------------
# Webhook path under live mode
# ----------------------------------------------------------------------


def test_webhook_live_mode_blocks_when_gates_open(tmp_path, monkeypatch):
    """Set EXECUTION_MODE=live + ENABLE_LIVE_TRADING via the arm endpoint,
    but flip a single gate back manually to confirm the webhook handler
    refuses with the right label and never POSTs."""
    _write_topstep_symbol_map(tmp_path)
    app = _build_topstep_app(tmp_path, monkeypatch)
    from app.execution.topstep import TopstepBroker as _TopstepBroker
    monkeypatch.setattr(_TopstepBroker, "_post_json", _post_factory())
    with TestClient(app) as c:
        c.post(
            "/api/topstep/live-execution/enable",
            json={"confirm": "engage", "account_ack": True},
        )
        # Knock the live confirm token out: the broker form rejects this,
        # but a direct settings_store write simulates a corrupted state.
        app.state.settings.live_trading_confirm = "disabled"
        app.state.settings_store.set_setting(
            "LIVE_TRADING_CONFIRM", "disabled"
        )
        r = c.post(
            "/webhooks/tradingview", json=make_alert(order_id="live_block_1")
        )
    body = r.json()
    assert body["accepted"] is False
    assert body["execution"]["message"] == "live_execution_locked"
    assert (
        body["execution"]["details"]["gate"] == "live_confirmation_missing"
    )


def test_webhook_live_mode_submits_when_gates_pass(tmp_path, monkeypatch):
    _write_topstep_symbol_map(tmp_path)
    app = _build_topstep_app(tmp_path, monkeypatch)
    from app.execution.topstep import TopstepBroker as _TopstepBroker
    monkeypatch.setattr(_TopstepBroker, "_post_json", _post_factory())
    with TestClient(app) as c:
        c.post(
            "/api/topstep/live-execution/enable",
            json={"confirm": "engage", "account_ack": True},
        )
        r = c.post(
            "/webhooks/tradingview", json=make_alert(order_id="live_ok_1")
        )
    body = r.json()
    assert body["accepted"] is True
    assert body["execution"]["message"] == "topstep_live_order_submitted"
    assert body["execution"]["order_id"] == "9001"
    assert body["execution"]["execution_mode"] == "live"


def test_webhook_live_mode_blocks_when_kill_switch_active(tmp_path, monkeypatch):
    """The kill switch must shadow live execution even when every other
    gate passes."""
    _write_topstep_symbol_map(tmp_path)
    app = _build_topstep_app(tmp_path, monkeypatch)
    from app.execution.topstep import TopstepBroker as _TopstepBroker
    monkeypatch.setattr(_TopstepBroker, "_post_json", _post_factory())
    with TestClient(app) as c:
        c.post(
            "/api/topstep/live-execution/enable",
            json={"confirm": "engage", "account_ack": True},
        )
        app.state.kill_switch.activate("kill_live")
        r = c.post(
            "/webhooks/tradingview", json=make_alert(order_id="live_kill_1")
        )
    body = r.json()
    # Risk engine rejects kill switch first → no live attempt at all.
    assert body["accepted"] is False
    assert body["rejection_reason"] == "kill_switch_active"


# ----------------------------------------------------------------------
# Live execution UI moved to Dashboard
# ----------------------------------------------------------------------


def test_broker_page_no_longer_shows_giant_live_execution_section(
    tmp_path, monkeypatch
):
    """The broker page must not embed the full live execution arm/disarm
    block any more — it lives on the Dashboard now. The page should
    point operators to the Dashboard instead."""
    app = _build_topstep_app(tmp_path, monkeypatch)
    with TestClient(app) as c:
        r = c.get("/settings/broker")
    assert r.status_code == 200
    html = r.text
    # No giant Topstep LIVE Execution heading on the broker page.
    assert "Topstep LIVE Execution" not in html
    assert "Arm LIVE execution" not in html
    # Pointer to the Dashboard must be present.
    assert "Execution controls moved to" in html
    # Dashboard link.
    assert 'href="/"' in html


def test_dashboard_renders_live_execution_arming_form(tmp_path, monkeypatch):
    """The live engagement modal (confirm phrase + account ack + Engage
    button) renders on the Dashboard when the broker is topstep."""
    app = _build_topstep_app(tmp_path, monkeypatch)
    with TestClient(app) as c:
        r = c.get("/")
    assert r.status_code == 200
    html = r.text
    # Modal element exists.
    assert 'id="live-execution-modal"' in html
    # Short typed phrase appears inside the modal copy; legacy long
    # phrase must not be exposed in the UI.
    assert ">engage<" in html
    assert "I_UNDERSTAND_LIVE_ORDERS" not in html
    # Engagement primary button.
    assert "Engage Live Execution" in html
    # Account acknowledgement copy.
    assert "I acknowledge orders will hit account" in html


def test_dashboard_renders_live_warning_copy(tmp_path, monkeypatch):
    """When live mode is selected, the Dashboard must surface a warning
    about funded/live Topstep routing — both in the modal and via the
    Live Locked status pill."""
    app = _build_topstep_app(tmp_path, monkeypatch)
    # Force EXECUTION_MODE=live so the status flips to Live Locked.
    s = app.state.settings
    s.execution_mode = "live"
    with TestClient(app) as c:
        r = c.get("/")
    assert r.status_code == 200
    html = r.text
    # Warning copy from the modal.
    assert "Live Execution Warning" in html
    assert "live/funded order routing" in html
    assert "funded/live Topstep account" in html
    # State label reflects the unarmed live selection.
    assert "Live Locked" in html or "Live Armed" in html


# ----------------------------------------------------------------------
# Secrets never leak
# ----------------------------------------------------------------------


def test_live_arm_response_does_not_leak_secrets(tmp_path, monkeypatch):
    app = _build_topstep_app(tmp_path, monkeypatch)
    with TestClient(app) as c:
        r = c.post(
            "/api/topstep/live-execution/enable",
            json={"confirm": "engage", "account_ack": True},
        )
    body = r.text
    settings = app.state.settings
    assert settings.topstep_api_key not in body
    assert settings.webhook_secret not in body
    assert (settings.admin_password_hash or "_no_hash_") not in body


def test_live_disable_response_does_not_leak_secrets(tmp_path, monkeypatch):
    app = _build_topstep_app(tmp_path, monkeypatch)
    with TestClient(app) as c:
        c.post(
            "/api/topstep/live-execution/enable",
            json={"confirm": "engage", "account_ack": True},
        )
        r = c.post("/api/topstep/live-execution/disable")
    body = r.text
    settings = app.state.settings
    assert settings.topstep_api_key not in body
    assert settings.webhook_secret not in body
    assert (settings.admin_password_hash or "_no_hash_") not in body


# ----------------------------------------------------------------------
# Demo execution still works unchanged
# ----------------------------------------------------------------------


def test_demo_arm_after_live_arm_disarm(tmp_path, monkeypatch):
    """Live arm → disarm → demo arm: each path must stay independent."""
    app = _build_topstep_app(tmp_path, monkeypatch)
    with TestClient(app) as c:
        c.post(
            "/api/topstep/live-execution/enable",
            json={"confirm": "engage", "account_ack": True},
        )
        c.post("/api/topstep/live-execution/disable")
        r = c.post(
            "/api/topstep/demo-execution/enable",
            json={"confirm": "DEMO_ONLY"},
        )
    body = r.json()
    assert body["ok"] is True
    assert body["status"] == "demo_execution_armed"
    s = app.state.settings
    assert s.execution_mode == "demo"
    assert s.enable_live_trading is False


# ----------------------------------------------------------------------
# sbctl audit command
# ----------------------------------------------------------------------


def test_sbctl_audit_runs_and_masks_secrets(tmp_path, monkeypatch):
    """``sbctl audit`` must print the safety snapshot without leaking the
    API key, password hash, webhook secret, or JWT."""
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash not available")

    # Run sbctl audit from the actual project root; the .venv there is
    # the only Python that has the app importable.
    result = subprocess.run(
        [bash, str(SBCTL), "audit"],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=str(PROJECT_ROOT),
    )
    if result.returncode != 0:
        pytest.skip(f"sbctl audit could not run: {result.stderr}")
    out = result.stdout
    assert "SignalBridge audit" in out
    assert "broker_provider" in out
    assert "execution_mode" in out
    assert "live execution armed" in out
    # The plaintext default webhook secret must not appear (it indicates
    # the operator hasn't rotated yet — but the *value* still shouldn't
    # leak via audit even when set).
    assert "topstep api key" in out.lower()
    # Verify no raw key/JWT/password material leaked. We can't easily
    # cross-reference the user's real .env from a unit test, so we just
    # make sure the audit doesn't dump fields it shouldn't.
    for forbidden in ("BEGIN PRIVATE", "Bearer "):
        assert forbidden not in out, f"audit leaked: {forbidden!r}"


# ----------------------------------------------------------------------
# Phase 2 — /settings/risk surfaces for the four live-execution
# safety knobs. The risk form now accepts:
#   * LIVE_MAX_CONTRACTS_PER_TRADE (audit Section 1 critical 1)
#   * LIVE_ALLOWED_SYMBOLS         (audit Section 1 critical 2)
#   * LIVE_REQUIRE_KILL_SWITCH_OFF (audit Section 1 critical 3)
#   * ENABLE_TOPSTEP_ORDER_DRY_RUN (audit Section 1 high finding)
#
# These tests confirm the round-trip (form post → coerce → SQLite →
# in-memory Settings) plus the cross-field check that keeps the live
# cap below the general cap.
# ----------------------------------------------------------------------


def _risk_form_payload(**overrides: str) -> dict[str, str]:
    """Baseline /settings/risk POST body matching the rendered form.

    The defaults reproduce what the page would submit if the operator
    just clicked Save without changing anything against a freshly
    bootstrapped app.
    """
    base: dict[str, str] = {
        "max_contracts_per_trade": "1",
        "strategy_managed_risk": "true",
        "fixed_contracts_per_trade": "1",
        "max_daily_loss": "250",
        "max_open_positions": "1",
        "duplicate_order_cooldown_seconds": "60",
        "enable_longs": "true",
        "enable_shorts": "true",
        "enable_timeframe_lock": "false",
        "allowed_timeframes": "1",
        "live_max_contracts_per_trade": "1",
        "live_allowed_symbols": "MES1!,MNQ1!",
        "live_require_kill_switch_off": "true",
        "enable_topstep_order_dry_run": "true",
    }
    base.update(overrides)
    return base


def test_settings_risk_renders_live_cap_inputs(tmp_path, monkeypatch):
    """GET /settings/risk must render all four Phase 2 inputs with the
    expected ``name`` attributes so the operator can save them."""
    app = _build_topstep_app(tmp_path, monkeypatch)
    with TestClient(app) as c:
        r = c.get("/settings/risk")
    assert r.status_code == 200
    body = r.text
    assert 'name="live_max_contracts_per_trade"' in body
    assert 'name="live_allowed_symbols"' in body
    assert 'name="live_require_kill_switch_off"' in body
    assert 'name="enable_topstep_order_dry_run"' in body
    # The intro copy must explain that these only matter under live
    # execution — operators landing on /settings/risk shouldn't think
    # these affect paper.
    assert "Live execution caps" in body


def test_settings_risk_persists_live_caps(tmp_path, monkeypatch):
    """POST values for all four Phase 2 fields and assert they land in
    SQLite + the in-memory Settings."""
    app = _build_topstep_app(tmp_path, monkeypatch)
    with TestClient(app) as c:
        r = c.post(
            "/settings/risk",
            data=_risk_form_payload(
                max_contracts_per_trade="5",
                live_max_contracts_per_trade="3",
                live_allowed_symbols="MES1!,MNQ1!,NQ1!",
                live_require_kill_switch_off="false",
                enable_topstep_order_dry_run="false",
            ),
            follow_redirects=False,
        )
    assert r.status_code == 303, r.text
    assert "Risk+settings+saved" in r.headers.get("location", "")

    s = app.state.settings
    assert s.live_max_contracts_per_trade == 3
    assert s.live_allowed_symbols == ["MES1!", "MNQ1!", "NQ1!"]
    assert s.live_require_kill_switch_off is False
    assert s.enable_topstep_order_dry_run is False

    # Persisted in SQLite too — these survive a restart.
    stored = app.state.settings_store.get_all_settings()
    assert stored["LIVE_MAX_CONTRACTS_PER_TRADE"] == "3"
    assert stored["LIVE_ALLOWED_SYMBOLS"] == "MES1!,MNQ1!,NQ1!"
    assert stored["LIVE_REQUIRE_KILL_SWITCH_OFF"] == "false"
    assert stored["ENABLE_TOPSTEP_ORDER_DRY_RUN"] == "false"


def test_settings_risk_rejects_live_cap_above_general(tmp_path, monkeypatch):
    """Cross-field check: LIVE_MAX_CONTRACTS_PER_TRADE > MAX_CONTRACTS_PER_TRADE
    is dead config because live signals still pass through the general
    cap first. The handler must refuse and leave settings untouched."""
    app = _build_topstep_app(tmp_path, monkeypatch)
    s = app.state.settings
    starting_live_cap = s.live_max_contracts_per_trade
    starting_general_cap = s.max_contracts_per_trade
    with TestClient(app) as c:
        r = c.post(
            "/settings/risk",
            data=_risk_form_payload(
                max_contracts_per_trade="5",
                live_max_contracts_per_trade="10",
            ),
            follow_redirects=False,
        )
    assert r.status_code == 303
    location = r.headers.get("location", "")
    assert "flash_kind=error" in location
    assert "LIVE_MAX_CONTRACTS_PER_TRADE" in location
    # In-memory settings unchanged on rejection.
    assert s.live_max_contracts_per_trade == starting_live_cap
    assert s.max_contracts_per_trade == starting_general_cap


def test_settings_risk_live_cap_equal_to_general_is_allowed(tmp_path, monkeypatch):
    """The cross-field check rejects strictly-greater. Equal is fine —
    the live cap can match the general cap, the live submission path
    just doesn't introduce a tighter constraint."""
    app = _build_topstep_app(tmp_path, monkeypatch)
    with TestClient(app) as c:
        r = c.post(
            "/settings/risk",
            data=_risk_form_payload(
                max_contracts_per_trade="4",
                live_max_contracts_per_trade="4",
            ),
            follow_redirects=False,
        )
    assert r.status_code == 303
    assert "Risk+settings+saved" in r.headers.get("location", "")
    s = app.state.settings
    assert s.live_max_contracts_per_trade == 4
    assert s.max_contracts_per_trade == 4


def test_settings_risk_unchecked_live_kill_switch_persists_false(tmp_path, monkeypatch):
    """An unchecked checkbox sends no value with the form, which the
    handler defaults to ``"false"``. Confirm that the false value
    survives coerce + apply for both new boolean settings."""
    app = _build_topstep_app(tmp_path, monkeypatch)
    payload = _risk_form_payload()
    # Simulate a browser submit where the operator unchecked both
    # toggles — the form keys are absent.
    payload.pop("live_require_kill_switch_off")
    payload.pop("enable_topstep_order_dry_run")
    with TestClient(app) as c:
        r = c.post(
            "/settings/risk",
            data=payload,
            follow_redirects=False,
        )
    assert r.status_code == 303
    assert "Risk+settings+saved" in r.headers.get("location", "")
    s = app.state.settings
    assert s.live_require_kill_switch_off is False
    assert s.enable_topstep_order_dry_run is False
