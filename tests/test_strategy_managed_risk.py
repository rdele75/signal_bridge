"""Tests for the strategy-managed vs fixed sizing logic."""
from __future__ import annotations

import json

from .conftest import make_alert


# ---------------------------------------------------------------------
# Webhook behavior
# ---------------------------------------------------------------------


def test_strategy_managed_on_uses_alert_contracts(client):
    """When STRATEGY_MANAGED_RISK=true the alert's contracts flows
    straight through to the broker (within the max cap)."""
    client.app.state.settings.strategy_managed_risk = True
    client.app.state.settings.max_contracts_per_trade = 3
    client.app.state.settings.fixed_contracts_per_trade = 1

    r = client.post(
        "/webhooks/tradingview",
        json=make_alert(contracts="2", order_id="sm_on_1"),
    )
    body = r.json()
    assert body["accepted"] is True
    assert body["execution"]["contracts"] == 2


def test_strategy_managed_on_rejects_above_max(client):
    """alert.contracts > max → reject as contracts_above_max."""
    client.app.state.settings.strategy_managed_risk = True
    client.app.state.settings.max_contracts_per_trade = 3
    client.app.state.settings.fixed_contracts_per_trade = 1

    r = client.post(
        "/webhooks/tradingview",
        json=make_alert(contracts="5", order_id="sm_on_over"),
    )
    body = r.json()
    assert body["accepted"] is False
    assert "contracts_above_max" in body["rejection_reason"]


def test_strategy_managed_on_missing_contracts_rejected(client):
    """Strategy-managed with no alert contracts → reject."""
    client.app.state.settings.strategy_managed_risk = True
    client.app.state.settings.max_contracts_per_trade = 3

    payload = make_alert(order_id="sm_on_missing")
    payload["contracts"] = None

    r = client.post("/webhooks/tradingview", json=payload)
    body = r.json()
    assert body["accepted"] is False
    assert body["rejection_reason"] == "missing_or_invalid_alert_contracts"


def test_strategy_managed_off_uses_fixed_contracts(client):
    """Toggle off → fixed contracts is used; alert.contracts is ignored
    for sizing."""
    client.app.state.settings.strategy_managed_risk = False
    client.app.state.settings.max_contracts_per_trade = 3
    client.app.state.settings.fixed_contracts_per_trade = 1

    r = client.post(
        "/webhooks/tradingview",
        # Alert asks for 5 — would be over max in strategy-managed mode.
        json=make_alert(contracts="5", order_id="sm_off_1"),
    )
    body = r.json()
    assert body["accepted"] is True
    assert body["execution"]["contracts"] == 1


def test_strategy_managed_off_ignores_alert_zero_contracts(client):
    """Even when the alert sends 0/missing contracts, fixed mode still
    executes at fixed size."""
    client.app.state.settings.strategy_managed_risk = False
    client.app.state.settings.max_contracts_per_trade = 3
    client.app.state.settings.fixed_contracts_per_trade = 2

    r = client.post(
        "/webhooks/tradingview",
        json=make_alert(contracts="0", order_id="sm_off_zero"),
    )
    body = r.json()
    assert body["accepted"] is True
    assert body["execution"]["contracts"] == 2


def test_strategy_managed_off_journals_audit_fields(client):
    """The journal entry's execution_result should carry the sizing
    audit so operators can see alert vs executed contracts."""
    client.app.state.settings.strategy_managed_risk = False
    client.app.state.settings.max_contracts_per_trade = 3
    client.app.state.settings.fixed_contracts_per_trade = 1

    client.post(
        "/webhooks/tradingview",
        json=make_alert(contracts="5", order_id="sm_off_audit"),
    )

    rows = client.app.state.journal.list_recent_signals(limit=1)
    assert rows, "no journal row produced"
    # list_recent_signals returns a slim projection. Pull the full row
    # via latest_signal which includes execution_result.
    latest = client.app.state.journal.latest_signal()
    assert latest is not None
    parsed = json.loads(latest["execution_result"] or "{}")
    sizing = parsed.get("risk_sizing") or {}
    assert sizing.get("alert_contracts") == 5
    assert sizing.get("executed_contracts") == 1
    assert sizing.get("strategy_managed_risk") is False


def test_strategy_managed_on_journals_audit_fields(client):
    client.app.state.settings.strategy_managed_risk = True
    client.app.state.settings.max_contracts_per_trade = 3
    client.app.state.settings.fixed_contracts_per_trade = 1

    client.post(
        "/webhooks/tradingview",
        json=make_alert(contracts="2", order_id="sm_on_audit"),
    )
    latest = client.app.state.journal.latest_signal()
    parsed = json.loads(latest["execution_result"] or "{}")
    sizing = parsed.get("risk_sizing") or {}
    assert sizing.get("alert_contracts") == 2
    assert sizing.get("executed_contracts") == 2
    assert sizing.get("strategy_managed_risk") is True


# ---------------------------------------------------------------------
# Settings / form persistence
# ---------------------------------------------------------------------


def test_post_settings_risk_saves_strategy_managed_off(client):
    r = client.post(
        "/settings/risk",
        data={
            "allowed_symbols": "MES1!,MNQ1!",
            "max_contracts_per_trade": "3",
            "strategy_managed_risk": "false",
            "fixed_contracts_per_trade": "2",
            "max_daily_loss": "250",
            "max_open_positions": "1",
            "duplicate_order_cooldown_seconds": "60",
            "enable_longs": "true",
            "enable_shorts": "true",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "Risk+settings+saved" in r.headers["location"]

    s = client.app.state.settings
    assert s.strategy_managed_risk is False
    assert s.fixed_contracts_per_trade == 2

    stored = client.app.state.settings_store.get_all_settings()
    assert stored["STRATEGY_MANAGED_RISK"] == "false"
    assert stored["FIXED_CONTRACTS_PER_TRADE"] == "2"


def test_post_settings_risk_saves_strategy_managed_on(client):
    r = client.post(
        "/settings/risk",
        data={
            "allowed_symbols": "MES1!,MNQ1!",
            "max_contracts_per_trade": "3",
            "strategy_managed_risk": "true",
            "fixed_contracts_per_trade": "1",
            "max_daily_loss": "250",
            "max_open_positions": "1",
            "duplicate_order_cooldown_seconds": "60",
            "enable_longs": "true",
            "enable_shorts": "true",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert client.app.state.settings.strategy_managed_risk is True
    stored = client.app.state.settings_store.get_all_settings()
    assert stored["STRATEGY_MANAGED_RISK"] == "true"


def test_post_settings_risk_rejects_fixed_over_max(client):
    r = client.post(
        "/settings/risk",
        data={
            "allowed_symbols": "MES1!,MNQ1!",
            "max_contracts_per_trade": "2",
            "strategy_managed_risk": "false",
            "fixed_contracts_per_trade": "5",
            "max_daily_loss": "250",
            "max_open_positions": "1",
            "duplicate_order_cooldown_seconds": "60",
            "enable_longs": "true",
            "enable_shorts": "true",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    loc = r.headers["location"]
    assert "flash_kind=error" in loc
    # Settings should be untouched.
    s = client.app.state.settings
    assert s.fixed_contracts_per_trade == 1
    assert s.max_contracts_per_trade == 1


def test_post_settings_risk_rejects_fixed_zero(client):
    r = client.post(
        "/settings/risk",
        data={
            "allowed_symbols": "MES1!,MNQ1!",
            "max_contracts_per_trade": "3",
            "strategy_managed_risk": "false",
            "fixed_contracts_per_trade": "0",
            "max_daily_loss": "250",
            "max_open_positions": "1",
            "duplicate_order_cooldown_seconds": "60",
            "enable_longs": "true",
            "enable_shorts": "true",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "flash_kind=error" in r.headers["location"]


def test_risk_page_renders_strategy_managed_toggle(client):
    body = client.get("/settings/risk").text
    assert 'name="strategy_managed_risk"' in body
    assert 'name="fixed_contracts_per_trade"' in body
    assert "Risk settings are set by the strategy" in body


# ---------------------------------------------------------------------
# /api/status surfaces the new knobs
# ---------------------------------------------------------------------


def test_api_status_includes_sizing_fields(client):
    body = client.get("/api/status").json()
    assert "strategy_managed_risk" in body
    assert "fixed_contracts_per_trade" in body
    assert "max_contracts_per_trade" in body
    # Defaults: ON.
    assert body["strategy_managed_risk"] is True


def test_api_broker_status_includes_sizing_fields(client):
    body = client.get("/api/broker/status").json()
    assert "strategy_managed_risk" in body
    assert "fixed_contracts_per_trade" in body
    assert "max_contracts_per_trade" in body


# ---------------------------------------------------------------------
# Topstep order preview uses post-sizing contracts
# ---------------------------------------------------------------------


def test_topstep_order_preview_uses_fixed_size(make_app, tmp_path):
    """When BROKER_PROVIDER=topstep and strategy-managed risk is off,
    the dry-run order builder must use the fixed quantity (not the
    alert quantity)."""
    import json as _json
    from fastapi.testclient import TestClient

    # Need an explicit Topstep symbol mapping so the builder produces a
    # payload instead of refusing on symbol_mapping_missing.
    symbols_path = tmp_path / "symbols.json"
    symbols_path.write_text(
        _json.dumps({"MES1!": {"topstep": "CON.F.US.MES.M26"}})
    )

    app = make_app(provider="topstep")
    # Configure: topstep account, fixed sizing, generous max.
    app.state.settings.topstep_account_id = "12345"
    app.state.settings.selected_account_id = "12345"
    app.state.broker.account_id = "12345"
    app.state.settings.strategy_managed_risk = False
    app.state.settings.fixed_contracts_per_trade = 1
    app.state.settings.max_contracts_per_trade = 3
    # Re-point the symbol map to our temp file.
    from app.symbol_map import SymbolMap

    app.state.symbol_map = SymbolMap(symbols_path)
    app.state.handler.symbol_map = app.state.symbol_map

    with TestClient(app) as c:
        r = c.post(
            "/webhooks/tradingview",
            json=make_alert(contracts="3", order_id="ts_fixed_1"),
        )
        body = r.json()

    assert body["execution"]["broker"] == "topstep"
    # Dry-run preview is the default — message should say so.
    details = body["execution"].get("details") or {}
    # Final order size must equal the fixed setting, not the alert's 3.
    assert details.get("size") == 1
    assert body["execution"]["contracts"] == 1


def test_topstep_order_preview_uses_alert_size_in_strategy_mode(
    make_app, tmp_path
):
    import json as _json
    from fastapi.testclient import TestClient

    symbols_path = tmp_path / "symbols.json"
    symbols_path.write_text(
        _json.dumps({"MES1!": {"topstep": "CON.F.US.MES.M26"}})
    )

    app = make_app(provider="topstep")
    app.state.settings.topstep_account_id = "12345"
    app.state.settings.selected_account_id = "12345"
    app.state.broker.account_id = "12345"
    app.state.settings.strategy_managed_risk = True
    app.state.settings.fixed_contracts_per_trade = 1
    app.state.settings.max_contracts_per_trade = 5
    from app.symbol_map import SymbolMap

    app.state.symbol_map = SymbolMap(symbols_path)
    app.state.handler.symbol_map = app.state.symbol_map

    with TestClient(app) as c:
        r = c.post(
            "/webhooks/tradingview",
            json=make_alert(contracts="3", order_id="ts_strategy_1"),
        )
        body = r.json()

    details = body["execution"].get("details") or {}
    assert details.get("size") == 3
    assert body["execution"]["contracts"] == 3


# ---------------------------------------------------------------------
# Paper execution uses the post-sizing contracts
# ---------------------------------------------------------------------


def test_paper_execution_uses_fixed_size(client):
    client.app.state.settings.strategy_managed_risk = False
    client.app.state.settings.fixed_contracts_per_trade = 1
    client.app.state.settings.max_contracts_per_trade = 5

    r = client.post(
        "/webhooks/tradingview",
        json=make_alert(contracts="3", order_id="paper_fixed_1"),
    )
    body = r.json()
    assert body["accepted"] is True
    # Paper fill respects the post-sizing quantity.
    assert body["execution"]["contracts"] == 1
    assert body["execution"]["position_after"]["quantity"] == 1


def test_paper_execution_uses_alert_size_in_strategy_mode(client):
    client.app.state.settings.strategy_managed_risk = True
    client.app.state.settings.max_contracts_per_trade = 5

    r = client.post(
        "/webhooks/tradingview",
        json=make_alert(contracts="3", order_id="paper_strategy_1"),
    )
    body = r.json()
    assert body["accepted"] is True
    assert body["execution"]["contracts"] == 3
    assert body["execution"]["position_after"]["quantity"] == 3
