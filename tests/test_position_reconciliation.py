"""Tests for finding M4 — broker vs journal position reconciliation.

``/api/broker/positions/reconcile`` is a read-only endpoint that
surfaces differences between the live broker positions and the
journal's open-position state. It never auto-corrects — operator
decides.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient


def test_in_sync_when_broker_and_journal_match(client):
    """Paper broker with no positions → both sides empty → in_sync."""
    r = client.get("/api/broker/positions/reconcile")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["in_sync"] is True
    assert body["differences"] == []
    assert body["broker_positions"] == []
    assert body["journal_positions"] == []


def test_qty_mismatch_reported(client):
    """Journal has MES1!:1, broker mock returns MES1!:2 — mismatch."""
    journal = client.app.state.journal
    journal.upsert_position(
        symbol="MES1!", quantity=1, avg_price=5000.0, side="long"
    )

    # Patch the broker.get_positions on the LIVE handler+broker.
    broker = client.app.state.broker
    broker.get_positions = MagicMock(
        return_value={
            "ok": True,
            "provider": broker.provider,
            "positions": [{"symbol": "MES1!", "size": 2}],
        }
    )

    body = client.get("/api/broker/positions/reconcile").json()
    assert body["ok"] is True
    assert body["in_sync"] is False
    diffs = {d["symbol"]: d for d in body["differences"]}
    assert "MES1!" in diffs
    assert diffs["MES1!"]["kind"] == "qty_mismatch"
    assert diffs["MES1!"]["broker_quantity"] == 2
    assert diffs["MES1!"]["journal_quantity"] == 1


def test_not_in_broker_reported(client):
    """Journal has a position the broker doesn't return."""
    journal = client.app.state.journal
    journal.upsert_position(
        symbol="MNQ1!", quantity=1, avg_price=21000.0, side="long"
    )
    broker = client.app.state.broker
    broker.get_positions = MagicMock(
        return_value={
            "ok": True,
            "provider": broker.provider,
            "positions": [],
        }
    )

    body = client.get("/api/broker/positions/reconcile").json()
    assert body["in_sync"] is False
    diffs = {d["symbol"]: d for d in body["differences"]}
    assert diffs["MNQ1!"]["kind"] == "not_in_broker"
    assert diffs["MNQ1!"]["journal_quantity"] == 1


def test_not_in_journal_reported(client):
    """Broker reports a position the journal doesn't know about."""
    broker = client.app.state.broker
    broker.get_positions = MagicMock(
        return_value={
            "ok": True,
            "provider": broker.provider,
            "positions": [
                {"contractId": "CON.F.US.MES.M26", "size": 2},
            ],
        }
    )

    body = client.get("/api/broker/positions/reconcile").json()
    assert body["in_sync"] is False
    diffs = {d["symbol"]: d for d in body["differences"]}
    assert diffs["CON.F.US.MES.M26"]["kind"] == "not_in_journal"
    assert diffs["CON.F.US.MES.M26"]["broker_quantity"] == 2


def test_broker_unreachable_does_not_crash(client):
    """Broker get_positions returning an ok=false envelope still
    yields a 200 envelope from the reconcile endpoint — UI must always
    be able to render."""
    broker = client.app.state.broker
    broker.get_positions = MagicMock(
        return_value={
            "ok": False,
            "status": "missing_credentials",
            "provider": broker.provider,
            "positions": [],
        }
    )

    body = client.get("/api/broker/positions/reconcile").json()
    assert body["ok"] is True
    assert body["broker_reachable"] is False
    assert body["broker_status"] == "missing_credentials"
    # Journal-side empty too → in_sync.
    assert body["in_sync"] is True


def test_endpoint_requires_admin_when_auth_enabled(tmp_path, monkeypatch):
    """The reconcile endpoint must be admin-gated like the rest of
    /api/broker/*."""
    from .conftest import _build_app

    app = _build_app(
        tmp_path, monkeypatch, provider="paper", admin_auth_enabled=True
    )
    with TestClient(app) as c:
        r = c.get("/api/broker/positions/reconcile")
    assert r.status_code == 401


def test_dashboard_no_longer_renders_reconcile_card(client):
    """The reconcile card was removed from the dashboard during the
    polish pass. The /api/broker/positions/reconcile endpoint stays
    (covered above) but the UI surface is gone."""
    body = client.get("/").text
    assert 'id="btn-reconcile-positions"' not in body
    assert 'id="reconcile-output"' not in body
    assert "Broker vs journal positions" not in body
