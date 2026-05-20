"""Tests for finding M1 — enforce ``canTrade`` on Topstep submission.

docs/audit.md claims "Selected Topstep account exists and (when
reported) ``canTrade=true``" for both demo and live execution. The
code didn't consult the flag at all. The adapter now caches the value
from ``get_accounts()`` and the safety check refuses with
``account_cannot_trade`` when the cached value is False. When no
snapshot has been cached the gate bypasses with a one-shot WARNING —
matches the "if known" qualifier in the docs.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from app.execution.topstep import TopstepBroker
from app.schemas import NormalizedSignal


def _broker(*, account_id: str = "5001", live: bool = False) -> TopstepBroker:
    """Construct a broker with safety gates lined up for execution so
    the canTrade check is the only remaining barrier."""
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    if live:
        return TopstepBroker(
            username="trader@example.com",
            api_key="abcd1234efgh5678",
            account_id=account_id,
            token="seed",
            token_expires_at=future,
            enable_order_execution=True,
            execution_confirm="LIVE_CONFIRMED",
            enable_live_trading=True,
            execution_mode="live",
            live_trading_confirm="I_UNDERSTAND_LIVE_ORDERS",
            live_trading_account_ack=True,
            live_allowed_symbols=["MES1!"],
            live_max_contracts_per_trade=5,
            max_contracts_per_trade=5,
        )
    return TopstepBroker(
        username="trader@example.com",
        api_key="abcd1234efgh5678",
        account_id=account_id,
        token="seed",
        token_expires_at=future,
        enable_order_execution=True,
        execution_confirm="DEMO_ONLY",
        execution_mode="demo",
        live_allowed_symbols=["MES1!"],
        max_contracts_per_trade=5,
    )


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
        order_id="m1_test",
        comment=None,
        timeframe=None,
        raw={},
    )


def test_can_trade_cache_populated_from_get_accounts():
    """A successful get_accounts() seeds the canTrade cache from the
    returned ``canTrade`` field."""
    broker = _broker()
    accounts_body = {
        "success": True,
        "errorCode": 0,
        "accounts": [
            {"id": 5001, "name": "PRACTICEMAY1", "canTrade": True, "isVisible": True},
            {"id": 5002, "name": "PRACTICEMAY2", "canTrade": False, "isVisible": True},
        ],
    }
    with patch.object(
        TopstepBroker,
        "_post_json",
        lambda self, path, payload, *, auth=False: (200, accounts_body),
    ):
        resp = broker.get_accounts()
    assert resp["ok"] is True
    assert broker._can_trade_cache == {"5001": True, "5002": False}


def test_demo_gate_rejects_when_can_trade_is_false():
    broker = _broker(account_id="5002")
    broker._can_trade_cache = {"5002": False}
    assert broker._demo_execution_safety_check() == "account_cannot_trade"


def test_live_gate_rejects_when_can_trade_is_false():
    broker = _broker(account_id="5002", live=True)
    broker._can_trade_cache = {"5002": False}
    gate = broker._live_execution_safety_check(_signal())
    assert gate == "account_cannot_trade"


def test_demo_gate_passes_when_can_trade_true():
    broker = _broker(account_id="5001")
    broker._can_trade_cache = {"5001": True}
    assert broker._demo_execution_safety_check() is None


def test_demo_gate_unknown_passes_with_warning_once(caplog):
    """No cached snapshot — gate falls open ("if known") but logs a
    WARNING the first time it does so, and only the first time."""
    broker = _broker(account_id="5001")
    assert broker._can_trade_cache == {}

    with caplog.at_level(logging.WARNING, logger="signalbridge.broker.topstep"):
        first = broker._demo_execution_safety_check()
        second = broker._demo_execution_safety_check()

    assert first is None
    assert second is None
    warning_messages = [
        r.message for r in caplog.records
        if r.levelno == logging.WARNING and "canTrade gate is unenforced" in r.message
    ]
    assert len(warning_messages) == 1, (
        f"expected exactly one WARNING, got {warning_messages!r}"
    )


def test_submit_market_order_refuses_when_can_trade_false():
    """End-to-end: a cached canTrade=False blocks the submission with
    ``account_cannot_trade``, no broker POST ever fires."""
    broker = _broker(account_id="5002")
    broker._can_trade_cache = {"5002": False}

    posts: list = []

    def fake_post(self, path, payload, *, auth=False):
        posts.append(path)
        return 200, {"success": True, "orderId": 1}

    with patch.object(TopstepBroker, "_post_json", fake_post):
        result = broker.submit_market_order(_signal())

    assert result["accepted"] is False
    assert result["gate"] == "account_cannot_trade"
    assert posts == []
