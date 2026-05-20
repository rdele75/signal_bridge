"""Tests for finding M5 — webhook rate limit.

A misconfigured TradingView alert template firing 100/s in a tight
loop would saturate the broker integration. /webhooks/tradingview now
uses a process-local token bucket: refused requests come back as 429
and are journaled as ``rate_limited`` rejections so they're visible
in the operator's audit trail.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.rate_limiter import TokenBucket

from .conftest import _build_app, make_alert


def _build_rate_limited_app(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    rate: float,
    burst: int,
):
    monkeypatch.setenv("WEBHOOK_RATE_LIMIT_PER_SECOND", str(rate))
    monkeypatch.setenv("WEBHOOK_RATE_BURST", str(burst))
    return _build_app(tmp_path, monkeypatch, provider="paper")


# -------------------- TokenBucket primitive ----------------------------


def test_token_bucket_admits_up_to_burst():
    b = TokenBucket(rate_per_second=1, burst=3)
    assert b.allow() is True
    assert b.allow() is True
    assert b.allow() is True
    assert b.allow() is False  # bucket empty


def test_token_bucket_refills_over_time():
    """After waiting > 1/rate seconds the bucket re-admits."""
    b = TokenBucket(rate_per_second=100, burst=1)
    assert b.allow() is True
    assert b.allow() is False
    # Wait long enough to refill a single token (10ms at 100/s = 1 token).
    time.sleep(0.05)
    assert b.allow() is True


def test_token_bucket_rejects_invalid_args():
    with pytest.raises(ValueError):
        TokenBucket(rate_per_second=0, burst=1)
    with pytest.raises(ValueError):
        TokenBucket(rate_per_second=1, burst=0)


# -------------------- /webhooks/tradingview integration ----------------


def test_webhook_rate_limit_returns_429_after_burst(tmp_path, monkeypatch):
    """Fire burst+1 requests with a slow refill rate; the last one 429s."""
    app = _build_rate_limited_app(
        tmp_path, monkeypatch, rate=0.001, burst=2
    )
    with TestClient(app) as c:
        statuses = []
        for i in range(3):
            r = c.post(
                "/webhooks/tradingview",
                json=make_alert(order_id=f"rl_{i}"),
            )
            statuses.append(r.status_code)
    # First two admitted (200 with possibly rejected business reasons,
    # but never 429). Third comes back 429.
    assert 429 not in statuses[:2], statuses
    assert statuses[-1] == 429, statuses


def test_webhook_rate_limit_response_body(tmp_path, monkeypatch):
    """The 429 body carries decision=rejected + reason=rate_limited so
    upstream tooling can react in a structured way."""
    app = _build_rate_limited_app(
        tmp_path, monkeypatch, rate=0.001, burst=1
    )
    with TestClient(app) as c:
        c.post("/webhooks/tradingview", json=make_alert(order_id="rl_one"))
        r = c.post(
            "/webhooks/tradingview", json=make_alert(order_id="rl_two")
        )
    assert r.status_code == 429
    body = r.json()
    assert body["accepted"] is False
    assert body["decision"] == "rejected"
    assert body["rejection_reason"] == "rate_limited"


def test_webhook_rate_limit_journals_refusal(tmp_path, monkeypatch):
    """Refused-by-rate-limit deliveries must appear in the journal as
    ``rate_limited`` rejections, otherwise they'd be invisible in the
    operator's audit trail."""
    app = _build_rate_limited_app(
        tmp_path, monkeypatch, rate=0.001, burst=1
    )
    with TestClient(app) as c:
        c.post("/webhooks/tradingview", json=make_alert(order_id="rlj_one"))
        c.post("/webhooks/tradingview", json=make_alert(order_id="rlj_two"))
    journal = app.state.journal
    reasons = journal.rejection_reasons(limit=20)
    rate_rows = [r for r in reasons if r["reason"] == "rate_limited"]
    assert rate_rows, f"expected a rate_limited journal row, got {reasons!r}"


def test_webhook_default_limits_dont_break_normal_traffic(client):
    """Smoke check: the default 10/s + 30 burst easily accommodates a
    handful of sequential test posts so the existing suite still
    passes."""
    for i in range(5):
        r = client.post(
            "/webhooks/tradingview",
            json=make_alert(order_id=f"normal_{i}"),
        )
        assert r.status_code != 429
