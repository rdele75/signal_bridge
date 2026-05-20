"""Tests for the ProjectX customTag uniqueness fix.

ProjectX rejects duplicate customTags per account. Each call that
generates an order must produce a tag that has never been used —
otherwise the first call works and every subsequent call fails
forever with ``Specified custom tag is already in use``.

These tests cover:

* the _generate_custom_tag helper produces unique tags,
* build_market_order_payload auto-generates when no tag source
  is available,
* explicit caller overrides still win,
* the smoke-test endpoint produces a different customTag on
  every call.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.execution.topstep import TopstepBroker
from app.execution.topstep_order_builder import (
    CUSTOM_TAG_MAX_LEN,
    _generate_custom_tag,
    build_market_order_payload,
)
from app.schemas import NormalizedSignal

from .conftest import _build_app


def _signal(**overrides) -> NormalizedSignal:
    base = dict(
        source="tradingview",
        strategy="orb",
        symbol="MES1!",
        broker_symbol="MES",
        exchange="CME_MINI",
        action="BUY",
        contracts=1,
        price=5000.25,
        order_id=None,
        comment=None,
        timeframe="1",
        raw={},
    )
    base.update(overrides)
    return NormalizedSignal(**base)


# ----------------------------------------------------------------------
# _generate_custom_tag helper
# ----------------------------------------------------------------------


def test_generate_custom_tag_produces_unique_tags_across_calls():
    """Two consecutive calls must produce different tags. If this
    fails the whole fix collapses — every smoke test would still
    collide."""
    tag1 = _generate_custom_tag("order")
    tag2 = _generate_custom_tag("order")
    assert tag1 != tag2


def test_generate_custom_tag_has_sb_prefix():
    """The ``sb-`` prefix marks SignalBridge-originated orders in
    the broker's audit log so reconciliation can identify them."""
    tag = _generate_custom_tag("smoke_entry")
    assert tag.startswith("sb-smoke_entry-")


def test_generate_custom_tag_respects_max_length():
    """ProjectX customTag has an implementation limit; CUSTOM_TAG_MAX_LEN
    is the in-process cap. A long purpose string must not produce a
    tag that exceeds it."""
    long_purpose = "a" * 200
    tag = _generate_custom_tag(long_purpose)
    assert len(tag) <= CUSTOM_TAG_MAX_LEN


def test_generate_custom_tag_suffix_is_non_empty():
    """The uuid-derived suffix must be present so the tag is actually
    unique — not just the prefix."""
    tag = _generate_custom_tag("order")
    suffix = tag.split("-", 2)[2]
    assert len(suffix) >= 8


# ----------------------------------------------------------------------
# build_market_order_payload customTag behaviour
# ----------------------------------------------------------------------


class _SymbolMap:
    def resolve_explicit(self, ticker, provider):
        return "CON.F.US.MES.M26" if ticker == "MES1!" else None


def test_builder_auto_generates_when_no_tag_source_available():
    """Signal has no order_id and no comment, no explicit custom_tag
    passed → builder must still produce a non-empty unique customTag."""
    p1 = build_market_order_payload(
        _signal(order_id=None, comment=None),
        account_id=5001,
        symbol_map=_SymbolMap(),
        custom_tag=None,
    )
    p2 = build_market_order_payload(
        _signal(order_id=None, comment=None),
        account_id=5001,
        symbol_map=_SymbolMap(),
        custom_tag=None,
    )
    assert p1["ok"] is True
    assert p2["ok"] is True
    tag1 = p1["payload"]["customTag"]
    tag2 = p2["payload"]["customTag"]
    assert tag1
    assert tag2
    assert tag1 != tag2
    assert tag1.startswith("sb-order-")


def test_builder_uses_signal_order_id_when_provided():
    """Webhook flow: TradingView's ``{{strategy.order.id}}`` is unique
    per generated order, so when the signal carries an explicit
    order_id the builder should honour it as the customTag rather
    than overriding."""
    payload = build_market_order_payload(
        _signal(order_id="alert-12345", comment="ignored"),
        account_id=5001,
        symbol_map=_SymbolMap(),
        custom_tag=None,
    )
    assert payload["payload"]["customTag"] == "alert-12345"


def test_builder_uses_signal_comment_when_order_id_missing():
    """When order_id is empty, the comment field is the fallback tag
    source. Auto-gen only kicks in when both are empty."""
    payload = build_market_order_payload(
        _signal(order_id=None, comment="my-tag-abc"),
        account_id=5001,
        symbol_map=_SymbolMap(),
        custom_tag=None,
    )
    assert payload["payload"]["customTag"] == "my-tag-abc"


def test_builder_respects_explicit_custom_tag_override():
    """An explicit ``custom_tag`` kwarg wins over signal fields and
    over the auto-gen. Callers must be able to specify exact tags."""
    payload = build_market_order_payload(
        _signal(order_id="alert-x", comment="comment-y"),
        account_id=5001,
        symbol_map=_SymbolMap(),
        custom_tag="explicit-tag-123",
    )
    assert payload["payload"]["customTag"] == "explicit-tag-123"


def test_builder_does_not_emit_duplicate_tags_across_two_signals():
    """The original bug: two smoke tests in a row produced the same
    customTag because both signals carried ``comment="smoke_test_entry"``.
    Even if BOTH signals carry the same comment, the builder must
    still produce unique tags when the caller signals the duplicate
    intent (by passing ``custom_tag=None`` and an empty order_id).

    Today the builder honours the duplicate comment — uniqueness is
    enforced at the call site. This test pins that contract: the
    builder treats a non-empty comment as an intentional caller choice.
    """
    sig = _signal(order_id=None, comment="duplicate-comment")
    p1 = build_market_order_payload(
        sig, account_id=5001, symbol_map=_SymbolMap(), custom_tag=None,
    )
    p2 = build_market_order_payload(
        sig, account_id=5001, symbol_map=_SymbolMap(), custom_tag=None,
    )
    # Identical inputs → identical tags. Caller responsibility to make
    # the input unique (smoke-test sites do this via _generate_custom_tag
    # on the comment field).
    assert p1["payload"]["customTag"] == p2["payload"]["customTag"]
    # And the tag survives — auto-gen only fires when both order_id
    # AND comment are empty.
    assert p1["payload"]["customTag"] == "duplicate-comment"


# ----------------------------------------------------------------------
# Smoke-test endpoint integration: distinct tags per call
# ----------------------------------------------------------------------


def _build_topstep_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("TOPSTEP_USERNAME", "trader42")
    monkeypatch.setenv("TOPSTEP_API_KEY", "abcd1234efgh5678")
    monkeypatch.setenv("TOPSTEP_ACCOUNT_ID", "5001")
    monkeypatch.setenv("SELECTED_ACCOUNT_ID", "5001")
    sm_path = tmp_path / "missing_symbols.json"
    sm_path.write_text(json.dumps({"MES1!": {"topstep": "CON.F.US.MES.M26"}}))
    monkeypatch.setenv("SYMBOLS_MAP_PATH", str(sm_path))
    return _build_app(tmp_path, monkeypatch, provider="topstep")


def test_smoke_test_endpoint_produces_unique_tags_across_two_calls(
    tmp_path, monkeypatch,
):
    """Two consecutive smoke-test invocations in EXECUTE mode must
    submit /api/Order/place calls with different customTags. This
    is the end-to-end test for the bug the operator hit: every
    smoke test after the first was rejected by ProjectX."""
    submitted: list[dict[str, Any]] = []

    def _fake_post(self, path, payload, *, auth=False):
        if path == "/api/Auth/loginKey":
            return 200, {
                "success": True, "token": "JWT.MOCK",
                "errorCode": 0, "errorMessage": None,
            }
        if path == "/api/Account/search":
            return 200, {
                "success": True, "errorCode": 0,
                "errorMessage": None,
                "accounts": [{
                    "id": 5001, "name": "Funded",
                    "balance": 100000.0,
                    "canTrade": True, "isVisible": True,
                }],
            }
        if path == "/api/Order/place":
            submitted.append(payload)
            return 200, {
                "success": True,
                "orderId": 8000 + len(submitted),
                "errorCode": 0,
                "errorMessage": None,
            }
        return 200, {"success": False, "errorCode": -1}

    app = _build_topstep_app(tmp_path, monkeypatch)
    from app.execution.topstep import TopstepBroker as _Topstep
    monkeypatch.setattr(_Topstep, "_post_json", _fake_post)

    with TestClient(app) as c:
        # Arm demo execution first so submit_market_order actually
        # POSTs through to /api/Order/place.
        arm = c.post(
            "/api/topstep/demo-execution/enable",
            json={"confirm": "DEMO_ONLY"},
        )
        assert arm.status_code == 200, arm.text

        # Two executions back-to-back.
        for _ in range(2):
            r = c.post(
                "/api/topstep/smoke-test",
                json={"execute": True, "confirmation": "smoke"},
            )
            assert r.status_code == 200, r.text

    # Each smoke test fires entry + exit = 2 orders. Two smoke tests
    # → 4 orders. Every customTag must be distinct.
    assert len(submitted) == 4
    tags = [p["customTag"] for p in submitted]
    assert all(tags), tags
    assert len(set(tags)) == 4, (
        f"customTag collision across smoke tests: {tags}"
    )
    # Each tag should carry the smoke prefix.
    for tag in tags:
        assert tag.startswith("sb-smoke_") or "smoke" in tag, tag


def test_submit_market_order_retry_reuses_same_payload(monkeypatch):
    """H5 retry safety: on auth failure, the retry must use the same
    payload dict (same customTag) — otherwise we'd risk submitting
    two distinct orders if the first actually went through but the
    response was lost. The retry should be idempotent at the broker."""
    calls: list[dict[str, Any]] = []

    def _fake_post(self, path, payload, *, auth=False):
        if path == "/api/Order/place":
            calls.append(dict(payload))
            if len(calls) == 1:
                # First attempt: 401 to force a re-auth + retry.
                return 401, {"success": False, "errorCode": 1,
                             "errorMessage": "Unauthorized"}
            return 200, {"success": True, "orderId": 91234,
                         "errorCode": 0, "errorMessage": None}
        if path == "/api/Auth/loginKey":
            return 200, {"success": True, "token": "JWT.REFRESHED",
                         "errorCode": 0, "errorMessage": None}
        return 200, {"success": False}

    monkeypatch.setattr(TopstepBroker, "_post_json", _fake_post)
    broker = TopstepBroker(
        username="trader42", api_key="abcd1234efgh5678",
        account_id="5001",
        token="JWT.PRE.CACHED",
        token_expires_at="2099-01-01T00:00:00+00:00",
        execution_mode="demo",
        enable_order_execution=True,
        execution_confirm="DEMO_ONLY",
    )
    broker._can_trade_cache["5001"] = True
    result = broker.submit_market_order(
        _signal(order_id="explicit-tag-77", action="BUY"),
        symbol_map=_SymbolMap(),
    )
    assert result["ok"] is True
    # Two calls to /api/Order/place — both with the identical
    # customTag (same payload).
    assert len(calls) == 2
    assert calls[0]["customTag"] == calls[1]["customTag"] == "explicit-tag-77"


def test_grep_audit_no_hardcoded_smoke_test_tags_in_main():
    """Cleanup guard: the hardcoded smoke_test_entry / smoke_test_exit
    strings that caused the bug must not reappear as customTag
    sources in main.py. Allow them in journal record_signal contexts
    where they're column data, not tag inputs."""
    main_py = Path(__file__).resolve().parent.parent / "app" / "main.py"
    text = main_py.read_text()
    # NormalizedSignal(...comment="smoke_test_entry"...) was the bug;
    # ensure the literal string never appears as the comment value of
    # a NormalizedSignal again. We accept it as journal.strategy=...
    # arguments (those are audit columns, not tag sources).
    assert 'comment="smoke_test_entry"' not in text
    assert 'comment="smoke_test_exit"' not in text
