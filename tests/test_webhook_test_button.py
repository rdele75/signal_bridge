"""Tests for the TradingView Test webhook button + its short-circuit
in WebhookHandler.

The endpoint at /api/tradingview/test-webhook dispatches a synthetic
``webhook_test=true`` payload through the same WebhookHandler the
live webhook uses. The handler must short-circuit on that flag —
no risk-engine call, no broker call, no journal write — and must
still require a valid secret to do so.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from .conftest import _build_app, login_as_admin, SECRET


def test_test_webhook_endpoint_returns_ok_with_configured_secret(client):
    """POST → 200 with ok=true and a structured envelope."""
    r = client.post("/api/tradingview/test-webhook")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["http_status"] == 200
    assert "Webhook is reachable" in body["message"]
    assert "response_body" in body
    # The decision inside the embedded WebhookResponse was webhook_test.
    assert "webhook_test" in body["response_body"]


def test_test_webhook_endpoint_requires_admin_auth(tmp_path, monkeypatch):
    app = _build_app(
        tmp_path, monkeypatch, provider="paper", admin_auth_enabled=True
    )
    with TestClient(app) as c:
        r = c.post("/api/tradingview/test-webhook")
    assert r.status_code == 401


def test_test_webhook_uses_configured_secret_not_hardcoded(
    tmp_path, monkeypatch
):
    """Rotating the secret without restarting must immediately affect
    the test endpoint — i.e. the endpoint reads
    ``settings.webhook_secret`` at request time, not at boot."""
    app = _build_app(tmp_path, monkeypatch, provider="paper")
    # First call with default test fixture secret succeeds.
    with TestClient(app) as c:
        ok = c.post("/api/tradingview/test-webhook").json()
        assert ok["ok"] is True
        # Rotate the secret in the live settings.
        app.state.settings.webhook_secret = "different_secret_99999999"
        again = c.post("/api/tradingview/test-webhook").json()
    # The new secret is what the handler now validates against; the
    # endpoint must pick it up.
    assert again["ok"] is True
    assert "webhook_test" in again["response_body"]


def test_test_webhook_short_circuit_does_not_write_journal(client):
    """The whole point of the short-circuit is that webhook_test=true
    bypasses the journal. After firing it, the recent-signals query
    must NOT include a webhook_test row."""
    pre = client.app.state.journal.list_recent_signals(limit=50)
    pre_count = len(pre)
    r = client.post("/api/tradingview/test-webhook")
    assert r.status_code == 200
    post = client.app.state.journal.list_recent_signals(limit=50)
    assert len(post) == pre_count, (
        "webhook_test short-circuit must not write a journal row"
    )


def test_webhook_handler_short_circuit_requires_valid_secret(client):
    """Hitting /webhooks/tradingview directly with webhook_test=true
    AND a bad secret must be rejected as invalid_secret — same
    contract as a real alert."""
    r = client.post(
        "/webhooks/tradingview",
        json={"webhook_test": True, "secret": "wrong_secret_value"},
    )
    body = r.json()
    assert body["accepted"] is False
    assert body["rejection_reason"] == "invalid_secret"


def test_webhook_handler_short_circuit_accepts_with_valid_secret(client):
    """Direct hit with the correct secret + webhook_test=true returns
    the short-circuit envelope. accepted=true, decision=webhook_test."""
    r = client.post(
        "/webhooks/tradingview",
        json={"webhook_test": True, "secret": SECRET},
    )
    body = r.json()
    assert body["accepted"] is True
    assert body["decision"] == "webhook_test"


def test_tradingview_page_renders_test_webhook_button(client):
    body = client.get("/tradingview").text
    assert 'id="btn-test-webhook"' in body
    assert "Test webhook connection" in body
    assert 'id="test-webhook-result"' in body


def test_test_webhook_endpoint_clean_when_secret_unset(tmp_path, monkeypatch):
    """If no real secret is configured, the endpoint must NOT attempt
    a test that would always succeed — it returns a clear refusal
    envelope so the operator sees they need to set a secret first.

    Builds the app then overwrites the live settings secret to the
    placeholder; the conftest fixture always sets a real test secret
    via the env so we have to clear it post-boot."""
    app = _build_app(tmp_path, monkeypatch, provider="paper")
    app.state.settings.webhook_secret = "change_me_to_a_long_random_secret"
    with TestClient(app) as c:
        r = c.post("/api/tradingview/test-webhook")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert "secret" in body["message"].lower()
