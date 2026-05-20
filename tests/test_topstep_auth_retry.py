"""Tests for finding H5 — re-authenticate and retry once on mid-flight
auth failure during ``submit_market_order``.

The local token-validity check uses mint time + 23h TTL. That can
disagree with what ProjectX actually enforces at submission time. When
the first ``/api/Order/place`` POST returns an auth-rejection
indicator (HTTP 401 or a recognized ``errorCode``), the adapter
re-authenticates and retries the POST exactly once. Non-auth failures
are passed through untouched.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from app.execution.topstep import (
    TopstepBroker,
    _is_auth_failure,
)
from app.schemas import NormalizedSignal


def test_is_auth_failure_recognizes_401_and_error_codes():
    assert _is_auth_failure(401, {}) is True
    assert _is_auth_failure(200, {"errorCode": 1}) is True
    assert _is_auth_failure(200, {"errorCode": 3}) is True
    # Anything else is a business rejection, not an auth retry trigger.
    assert _is_auth_failure(200, {"errorCode": 5}) is False
    assert _is_auth_failure(500, {"errorCode": None}) is False
    assert _is_auth_failure(200, "non-json") is False


def _broker(*, signal_symbol: str = "MES1!") -> TopstepBroker:
    """Construct a broker primed to clear the safety gates and have a
    fresh local token so ``submit_market_order`` reaches the POST."""
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    b = TopstepBroker(
        username="trader@example.com",
        api_key="abcd1234efgh5678",
        account_id="5001",
        token="seed-token",
        token_expires_at=future,
        # Demo execution gates lined up so the safety check passes.
        enable_order_execution=True,
        execution_confirm="DEMO_ONLY",
        execution_mode="demo",
        live_allowed_symbols=[signal_symbol],
        max_contracts_per_trade=5,
    )
    return b


class _StubSymbolMap:
    """resolve_explicit always returns the same contract id so the
    order-builder check succeeds."""

    def resolve_explicit(self, ticker, provider):
        return "CON.F.US.MES.M26"


def _signal(symbol: str = "MES1!") -> NormalizedSignal:
    return NormalizedSignal(
        source="tradingview",
        strategy="t",
        symbol=symbol,
        broker_symbol=symbol,
        exchange=None,
        action="BUY",
        contracts=1,
        price=5000.0,
        order_id="h5_test",
        comment=None,
        timeframe=None,
        raw={},
    )


def test_auth_failure_triggers_one_reauth_and_retry():
    """First POST returns 401, second returns 200/success — and exactly
    one ``authenticate()`` call sits between them."""
    broker = _broker()
    smap = _StubSymbolMap()

    post_calls: list[dict] = []
    auth_calls: list[bool] = []

    def fake_post(self, path, payload, *, auth=False):
        post_calls.append({"path": path, "payload": payload})
        if len(post_calls) == 1:
            return 401, {"errorCode": 1, "errorMessage": "token expired"}
        return 200, {
            "success": True,
            "orderId": 999,
            "errorCode": 0,
            "errorMessage": None,
        }

    def fake_authenticate(self):
        auth_calls.append(True)
        # Reset the local token so the next POST proceeds.
        self.token = "fresh-token"
        return {"ok": True, "status": "authenticated"}

    with patch.object(TopstepBroker, "_post_json", fake_post), patch.object(
        TopstepBroker, "authenticate", fake_authenticate
    ):
        result = broker.submit_market_order(_signal(), symbol_map=smap)

    assert result["ok"] is True, result
    assert result["accepted"] is True
    assert result["broker_order_id"] == "999"
    assert len(post_calls) == 2, "expected exactly one retry"
    assert len(auth_calls) == 1, "expected exactly one re-auth"


def test_persistent_auth_failure_does_not_loop():
    """Both POSTs come back as auth-rejection — adapter must not retry
    again. Caller sees the failure envelope."""
    broker = _broker()
    smap = _StubSymbolMap()

    post_calls: list = []
    auth_calls: list = []

    def fake_post(self, path, payload, *, auth=False):
        post_calls.append(payload)
        return 401, {"errorCode": 1, "errorMessage": "still unauthorized"}

    def fake_authenticate(self):
        auth_calls.append(True)
        self.token = "fresh-token"
        return {"ok": True, "status": "authenticated"}

    with patch.object(TopstepBroker, "_post_json", fake_post), patch.object(
        TopstepBroker, "authenticate", fake_authenticate
    ):
        result = broker.submit_market_order(_signal(), symbol_map=smap)

    assert result["ok"] is False
    assert len(post_calls) == 2, "exactly one retry, never more"
    assert len(auth_calls) == 1


def test_non_auth_failure_does_not_retry():
    """A business rejection (e.g. risk-limit hit at the broker) must
    NOT trigger a re-auth + retry — that would mask real errors and
    burn the auth path during regular outages."""
    broker = _broker()
    smap = _StubSymbolMap()

    post_calls: list = []
    auth_calls: list = []

    def fake_post(self, path, payload, *, auth=False):
        post_calls.append(payload)
        return 200, {
            "success": False,
            "orderId": None,
            "errorCode": 7,
            "errorMessage": "instrument not tradeable right now",
        }

    def fake_authenticate(self):
        auth_calls.append(True)
        return {"ok": True}

    with patch.object(TopstepBroker, "_post_json", fake_post), patch.object(
        TopstepBroker, "authenticate", fake_authenticate
    ):
        result = broker.submit_market_order(_signal(), symbol_map=smap)

    assert result["ok"] is False
    assert result["status"] == "submit_rejected"
    assert len(post_calls) == 1, "no retry on non-auth rejection"
    assert auth_calls == []


def test_reauth_itself_fails_no_retry_attempted():
    """First POST is 401, authenticate() returns ok=false — adapter
    must not call _post_json a second time. The caller still gets a
    rejection envelope from the FIRST POST's data."""
    broker = _broker()
    smap = _StubSymbolMap()

    post_calls: list = []
    auth_calls: list = []

    def fake_post(self, path, payload, *, auth=False):
        post_calls.append(payload)
        return 401, {"errorCode": 1, "errorMessage": "expired"}

    def fake_authenticate(self):
        auth_calls.append(True)
        return {"ok": False, "status": "auth_failed", "message": "bad key"}

    with patch.object(TopstepBroker, "_post_json", fake_post), patch.object(
        TopstepBroker, "authenticate", fake_authenticate
    ):
        result = broker.submit_market_order(_signal(), symbol_map=smap)

    assert result["ok"] is False
    assert len(post_calls) == 1, "no retry when re-auth failed"
    assert len(auth_calls) == 1
