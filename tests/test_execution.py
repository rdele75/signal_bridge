"""Tests for the post-collapse off/test/armed execution model.

Covers the four invariants documented in the
execution-model-collapse-2026-05-21 brief:

  1. Test mode short-circuits — ``submit_market_order`` returns
     ``submitted=False, mode="test"`` and never POSTs to
     ``/api/Order/place``.
  2. Armed mode submits — exactly one POST per signal when every gate
     passes.
  3. The funded badge classification ("funded" / "eval" / "unknown")
     fires from the account name heuristic.
  4. ``ALLOWED_SYMBOLS_ARMED`` is enforced only when armed — Test
     signals using a non-armed-but-generally-allowed symbol pass; the
     same symbol in Armed mode is rejected.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.dashboard import _classify_funded
from app.execution.topstep import TopstepBroker
from app.schemas import NormalizedSignal

from .conftest import _build_app, make_alert


def _write_topstep_symbol_map(tmp_path: Path) -> None:
    sm_path = tmp_path / "missing_symbols.json"
    sm_path.write_text(
        json.dumps({"MES1!": {"topstep": "CON.F.US.MES.M26"}})
    )


def _signal(**overrides: Any) -> NormalizedSignal:
    base = dict(
        source="tradingview",
        strategy="orb",
        symbol="MES1!",
        broker_symbol="CON.F.US.MES.M26",
        exchange="CME_MINI",
        action="BUY",
        contracts=1,
        price=5000.25,
        order_id="execution_test_1",
        comment="execution unit test",
        timeframe="1",
        raw={},
    )
    base.update(overrides)
    return NormalizedSignal(**base)


def _post_factory():
    """Mint a fake ``_post_json`` that records every call. Auth and
    accounts endpoints succeed; everything else returns a generic
    success envelope. The test asserts that
    ``/api/Order/place`` is NOT in the recorded calls for Test mode."""
    calls: list[tuple[str, dict[str, Any]]] = []

    def _fake_post(self, path, payload, *, auth: bool = False):
        calls.append((path, payload))
        if path == "/api/Auth/loginKey":
            return 200, {
                "success": True, "token": "JWT.TOKEN",
                "errorCode": 0, "errorMessage": None,
            }
        if path == "/api/Account/search":
            return 200, {
                "success": True, "errorCode": 0, "errorMessage": None,
                "accounts": [{
                    "id": 5001, "name": "Funded", "balance": 100000.0,
                    "canTrade": True, "isVisible": True,
                }],
            }
        if path == "/api/Order/place":
            return 200, {
                "success": True, "orderId": 9001,
                "errorCode": 0, "errorMessage": None,
            }
        return 200, {"success": False, "errorCode": -1}

    return calls, _fake_post


# ----------------------------------------------------------------------
# Invariant 1: Test mode short-circuits before POSTing
# ----------------------------------------------------------------------


def test_test_mode_builds_payload_but_does_not_post(
    tmp_path, monkeypatch
):
    _write_topstep_symbol_map(tmp_path)
    app = _build_app(tmp_path, monkeypatch)
    app.state.settings.execution_mode = "test"
    broker = app.state.broker
    assert broker.provider == "topstep"
    broker.execution_mode = "test"
    # Pretend auth is fresh so no /loginKey call happens.
    broker.token = "JWT"
    broker.token_expires_at = "2099-01-01T00:00:00+00:00"

    calls, fake_post = _post_factory()
    monkeypatch.setattr(broker.__class__, "_post_json", fake_post)

    result = broker.submit_market_order(
        _signal(), symbol_map=app.state.symbol_map
    )
    assert result["ok"] is True
    assert result["submitted"] is False
    assert result["mode"] == "test"
    # Critically: no POST to /api/Order/place.
    placed_paths = [path for path, _ in calls]
    assert "/api/Order/place" not in placed_paths


# ----------------------------------------------------------------------
# Invariant 2: Armed mode submits exactly once
# ----------------------------------------------------------------------


def test_armed_mode_submits_exactly_one_order_place(
    tmp_path, monkeypatch
):
    _write_topstep_symbol_map(tmp_path)
    app = _build_app(tmp_path, monkeypatch)
    app.state.settings.execution_mode = "armed"
    broker = app.state.broker
    assert broker.provider == "topstep"
    broker.execution_mode = "armed"
    broker.token = "JWT"
    broker.token_expires_at = "2099-01-01T00:00:00+00:00"
    broker._can_trade_cache[str(broker.account_id)] = True
    # Defensive: the test-fixture credentials must be threaded into the
    # adapter. signal_router pulls them from Settings at construction
    # time; if env-bootstrap rejected an env value the broker can land
    # without credentials.
    if not broker.username:
        broker.username = "test_user@example.com"
    if not broker.api_key:
        broker.api_key = "test_api_key_abcd1234"

    calls, fake_post = _post_factory()
    monkeypatch.setattr(broker.__class__, "_post_json", fake_post)

    result = broker.submit_market_order(
        _signal(), symbol_map=app.state.symbol_map
    )
    import json as _json
    assert result["ok"] is True, _json.dumps(result, default=str, indent=2)
    assert result["submitted"] is True
    assert result["mode"] == "armed"
    assert result["broker_order_id"] == "9001"
    placed = [p for p, _ in calls if p == "/api/Order/place"]
    assert len(placed) == 1, calls


# ----------------------------------------------------------------------
# Invariant 3: Funded-badge classification heuristic
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,expected",
    [
        ("PRACTICEDEC1100146-21434541", False),
        ("EVAL-100K-account", False),
        ("Express Funded", False),  # Express is an eval product
        ("Trial-50K", False),
        ("DEMO-200K", False),
        ("SIM-Funded", False),  # SIM keyword wins
        ("Combine-150K", False),
        ("FundedAccount-1010", True),  # plain "Funded" without any blockers
        ("PA-50K", True),  # Performance Account — funded
        ("", None),
        (None, None),
    ],
)
def test_funded_classification_heuristic(name, expected):
    account = {"name": name} if name is not None else {}
    if name is None:
        account = {}
    assert _classify_funded(account) is expected


def test_funded_badge_surfaces_in_dashboard_context(tmp_path, monkeypatch):
    """The dashboard context exposes ``selected_account_is_funded`` so
    the template can render the Funded / Eval badge."""
    _write_topstep_symbol_map(tmp_path)
    app = _build_app(tmp_path, monkeypatch)
    broker = app.state.broker
    assert broker.provider == "topstep"

    # Bypass the live ProjectX probe with a canned response carrying a
    # Funded-shaped account name. broker_status_payload should pick up
    # the heuristic from the name alone.
    def fake_probe(self):  # noqa: ARG001 - called bound
        return {
            "ok": True,
            "connected": True,
            "provider": "topstep",
            "status": "ok",
            "message": "stub",
            "credentials": {"token_cached": True, "token_expires_at": ""},
            "accounts_count": 1,
            "selected_account_id": broker.account_id,
            "selected_account": {
                "id": 5001, "name": "FundedAccount-1010",
                "balance": 100000.0, "canTrade": True, "isVisible": True,
            },
        }
    monkeypatch.setattr(broker.__class__, "test_connection", fake_probe)

    from app.dashboard import broker_status_payload
    payload = broker_status_payload(
        settings=app.state.settings, broker=broker
    )
    assert payload["selected_account_is_funded"] is True
    assert payload["selected_account"]["name"] == "FundedAccount-1010"


def test_eval_badge_classification_via_dashboard_context(
    tmp_path, monkeypatch
):
    """Same surface as the funded test but with a PRACTICE-named
    account. ``selected_account_is_funded`` should be False (eval)."""
    _write_topstep_symbol_map(tmp_path)
    app = _build_app(tmp_path, monkeypatch)
    broker = app.state.broker

    def fake_probe(self):  # noqa: ARG001
        return {
            "ok": True,
            "connected": True,
            "provider": "topstep",
            "status": "ok",
            "message": "stub",
            "credentials": {},
            "accounts_count": 1,
            "selected_account_id": broker.account_id,
            "selected_account": {
                "id": 5001, "name": "PRACTICEDEC1100146-21434541",
                "balance": 100000.0, "canTrade": True, "isVisible": True,
            },
        }
    monkeypatch.setattr(broker.__class__, "test_connection", fake_probe)

    from app.dashboard import broker_status_payload
    payload = broker_status_payload(
        settings=app.state.settings, broker=broker
    )
    assert payload["selected_account_is_funded"] is False


# ----------------------------------------------------------------------
# Invariant 4: ALLOWED_SYMBOLS_ARMED enforced only when armed
# ----------------------------------------------------------------------


def test_armed_symbol_allowlist_blocks_non_armed_symbol(tmp_path, monkeypatch):
    """A symbol in ALLOWED_SYMBOLS but NOT in ALLOWED_SYMBOLS_ARMED
    passes risk in Test (where only the general allowlist applies) and
    is rejected by the risk engine in Armed."""
    _write_topstep_symbol_map(tmp_path)
    app = _build_app(tmp_path, monkeypatch)
    settings = app.state.settings
    # NQ1! is in the general allowlist (test fixture) but NOT in
    # allowed_symbols_armed (which is MES1!,MNQ1! only).
    assert "NQ1!" in settings.allowed_symbols
    assert "NQ1!" not in settings.allowed_symbols_armed
    risk = app.state.risk

    signal = _signal(symbol="NQ1!")

    settings.execution_mode = "test"
    decision = risk.evaluate(signal)
    assert decision.accepted is True, decision

    settings.execution_mode = "armed"
    decision = risk.evaluate(signal)
    assert decision.accepted is False
    assert decision.reason and decision.reason.startswith(
        "symbol_not_allowed_armed"
    )


# ----------------------------------------------------------------------
# Endpoint smoke tests for /api/execution/*
# ----------------------------------------------------------------------


def test_api_execution_off_sets_mode_off(tmp_path, monkeypatch):
    app = _build_app(tmp_path, monkeypatch)
    app.state.settings.execution_mode = "test"
    with TestClient(app) as c:
        r = c.post("/api/execution/off")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["execution_mode"] == "off"
    assert app.state.settings.execution_mode == "off"


def test_api_execution_test_sets_mode_test(tmp_path, monkeypatch):
    app = _build_app(tmp_path, monkeypatch)
    with TestClient(app) as c:
        r = c.post("/api/execution/test")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["execution_mode"] == "test"
    assert app.state.settings.execution_mode == "test"


def test_api_execution_arm_refuses_when_no_account(tmp_path, monkeypatch):
    """Without a selected account, /api/execution/arm refuses with a
    structured 400 and does NOT flip the setting."""
    monkeypatch.setenv("TOPSTEP_ACCOUNT_ID", "")
    monkeypatch.setenv("SELECTED_ACCOUNT_ID", "")
    app = _build_app(tmp_path, monkeypatch)
    assert not app.state.settings.resolved_account_id
    with TestClient(app) as c:
        r = c.post("/api/execution/armed")
    assert r.status_code == 400
    assert r.json()["status"] == "no_selected_account"
    assert app.state.settings.execution_mode != "armed"


def test_api_execution_arm_refuses_when_kill_switch_active(
    tmp_path, monkeypatch
):
    app = _build_app(tmp_path, monkeypatch)
    app.state.kill_switch.activate("test_block_arm")
    with TestClient(app) as c:
        r = c.post("/api/execution/armed")
    assert r.status_code == 400
    assert r.json()["status"] == "kill_switch_active"
    assert app.state.settings.execution_mode != "armed"


def test_api_execution_arm_success(tmp_path, monkeypatch):
    app = _build_app(tmp_path, monkeypatch)
    with TestClient(app) as c:
        r = c.post("/api/execution/armed")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["execution_mode"] == "armed"
    assert app.state.settings.execution_mode == "armed"


def test_armed_kill_switch_active_rejects_via_risk_engine(
    tmp_path, monkeypatch
):
    """When execution is Armed AND the kill switch is on, the risk
    engine rejects with kill_switch_active. (The arming endpoint
    refuses to flip Armed while the kill switch is hot — so this
    tests the runtime-after-arm path.)"""
    app = _build_app(tmp_path, monkeypatch)
    settings = app.state.settings
    settings.execution_mode = "armed"
    app.state.kill_switch.activate("test_runtime_kill")
    decision = app.state.risk.evaluate(_signal())
    assert decision.accepted is False
    assert decision.reason == "kill_switch_active"


def test_settings_broker_post_hot_reloads_credentials_onto_live_broker(
    tmp_path, monkeypatch
):
    """Saving credentials via /settings/broker must update the running
    broker without a restart. The cached auth token is cleared because
    the new credentials imply a new auth context.

    Regression: pre-polish the broker cached username/api_key at
    construction and ignored later DB writes, so test-connection kept
    reporting missing_credentials until restart.
    """
    # Build the app with empty Topstep credentials so the POST is a
    # real "first time saving credentials" interaction.
    monkeypatch.setenv("TOPSTEP_USERNAME", "")
    monkeypatch.setenv("TOPSTEP_API_KEY", "")
    monkeypatch.setenv("TOPSTEP_ACCOUNT_ID", "")
    monkeypatch.setenv("SELECTED_ACCOUNT_ID", "")
    app = _build_app(tmp_path, monkeypatch)
    broker = app.state.broker
    assert broker.provider == "topstep"
    assert broker.username == ""
    assert broker.api_key == ""

    # Seed a stale token so we can confirm the refresh wipes the cache.
    broker.token = "STALE.JWT"
    broker.token_expires_at = "2099-01-01T00:00:00+00:00"
    broker._can_trade_cache["old"] = True

    with TestClient(app) as c:
        r = c.post(
            "/settings/broker",
            data={
                "selected_account_id": "12345",
                "topstep_username": "new_user@example.com",
                "topstep_api_key": "new_api_key_value_0000",
                "topstep_env": "demo",
            },
            follow_redirects=False,
        )
    assert r.status_code == 303, r.text

    assert broker.username == "new_user@example.com"
    assert broker.api_key == "new_api_key_value_0000"
    assert broker.account_id == "12345"
    # New creds → invalidate cached auth artifacts.
    assert broker.token == ""
    assert broker.token_expires_at == ""
    assert broker._can_trade_cache == {}


def test_off_state_skips_broker(tmp_path, monkeypatch):
    """A valid webhook in Off state is journaled as accepted but the
    broker is never asked to execute. The result.message identifies
    the off-state bypass."""
    _write_topstep_symbol_map(tmp_path)
    app = _build_app(tmp_path, monkeypatch)
    app.state.settings.execution_mode = "off"
    calls, fake_post = _post_factory()
    monkeypatch.setattr(app.state.broker.__class__, "_post_json", fake_post)
    with TestClient(app) as c:
        r = c.post(
            "/webhooks/tradingview",
            json=make_alert(symbol="MES1!", order_id="off_test_1"),
        )
    body = r.json()
    assert body["accepted"] is True
    assert body["execution"]["message"] == "execution_off_no_submission"
    placed = [p for p, _ in calls if p == "/api/Order/place"]
    assert placed == []
