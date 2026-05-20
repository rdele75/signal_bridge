"""Concurrent-duplicate-order race tests for finding H1.

The webhook handler used to query ``find_recent_order_id`` and then
journal the result with no lock between the two operations. Two
near-simultaneous webhooks sharing an ``order_id`` could both pass the
duplicate check before either landed in the journal — so both reached
the broker. ``WebhookHandler._serialize_order_id`` now wraps the
risk-evaluate → broker → journal sequence in a per-order_id
``threading.Lock``; this file pins that behavior down.
"""
from __future__ import annotations

import threading
import time
from typing import Any

import pytest

from .conftest import make_alert


def _run_concurrent(handler, payloads, *, slow_broker_seconds: float = 0.05):
    """Fire ``handler.handle(payload)`` from one thread per payload.

    ``broker.execute`` is wrapped with a small sleep so the threads
    overlap meaningfully on the risk → broker → journal path. With the
    lock in place the second thread is forced to wait for the first to
    finish before its risk check runs.
    """
    broker = handler.broker
    real_execute = broker.execute

    def slow_execute(signal):
        time.sleep(slow_broker_seconds)
        return real_execute(signal)

    broker.execute = slow_execute
    try:
        results: list[Any] = [None] * len(payloads)

        def runner(idx, payload):
            results[idx] = handler.handle(payload)

        threads = [
            threading.Thread(target=runner, args=(i, p))
            for i, p in enumerate(payloads)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)
            assert not t.is_alive(), "handler.handle() hung — likely a lock leak"
        return results
    finally:
        broker.execute = real_execute


def test_same_order_id_concurrent_only_one_accepted(client):
    """Two threads, same order_id → one accepted, one duplicate_order_id."""
    handler = client.app.state.handler
    payload_a = make_alert(order_id="race_same")
    payload_b = make_alert(order_id="race_same")

    results = _run_concurrent(handler, [payload_a, payload_b])

    accepted = [r for r in results if r.accepted]
    rejected = [r for r in results if not r.accepted]
    assert len(accepted) == 1, f"expected 1 accepted, got {accepted!r}"
    assert len(rejected) == 1, f"expected 1 rejected, got {rejected!r}"
    assert rejected[0].rejection_reason == "duplicate_order_id"


def test_different_order_ids_concurrent_both_accepted(client):
    """Two threads, different order_ids → both accepted. The lock is
    per-order_id, not a global mutex, so unrelated signals don't
    serialize against each other."""
    handler = client.app.state.handler
    # Use different symbols too so the paper broker's per-symbol fill
    # path doesn't collide on max_open_positions.
    payload_a = make_alert(symbol="MES1!", order_id="race_diff_a")
    payload_b = make_alert(symbol="MNQ1!", order_id="race_diff_b")
    # Lift max_open_positions so the second symbol isn't refused.
    handler.settings.max_open_positions = 5
    handler.settings.allowed_symbols = ["MES1!", "MNQ1!"]

    results = _run_concurrent(handler, [payload_a, payload_b])

    accepted = [r for r in results if r.accepted]
    assert len(accepted) == 2, f"expected both accepted, got {results!r}"


def test_no_order_id_skips_lock(client):
    """Signals without ``order_id`` must not acquire the per-id lock —
    matches the existing duplicate-check behavior. Two concurrent
    no-id signals on different symbols both flow through normally."""
    handler = client.app.state.handler
    payload_a = make_alert(symbol="MES1!")
    payload_a.pop("order_id", None)
    payload_b = make_alert(symbol="MNQ1!")
    payload_b.pop("order_id", None)
    handler.settings.max_open_positions = 5
    handler.settings.allowed_symbols = ["MES1!", "MNQ1!"]

    results = _run_concurrent(handler, [payload_a, payload_b])

    accepted = [r for r in results if r.accepted]
    assert len(accepted) == 2, results
    # And no entry in the lock dict for the empty-string key.
    assert "" not in handler._order_id_locks
    assert None not in handler._order_id_locks


def test_lock_released_after_risk_rejection(client):
    """A risk rejection must release the per-id lock so a subsequent
    legitimate webhook with the same order_id (after the cooldown) can
    be accepted. ``try/finally`` in ``_serialize_order_id`` is what
    keeps this honest."""
    handler = client.app.state.handler
    # Force the first request to fail risk by disabling longs — but
    # only for the first call. Then re-enable.
    handler.settings.enable_longs = False
    bad = make_alert(order_id="lock_release_check")
    first = handler.handle(bad)
    assert first.accepted is False
    assert first.rejection_reason == "longs_disabled"

    handler.settings.enable_longs = True

    # If the lock leaked, this call would block forever. Bound the
    # wait so the test fails fast on a regression instead of hanging.
    done = threading.Event()
    result: dict = {}

    def go():
        try:
            result["resp"] = handler.handle(
                make_alert(order_id="lock_release_check_two")
            )
        finally:
            done.set()

    threading.Thread(target=go).start()
    assert done.wait(timeout=5.0), "handler.handle() hung — lock not released"
    assert result["resp"].accepted is True


def test_locks_dict_has_one_entry_per_order_id(client):
    """Verify the lock-dict's bookkeeping. Two calls with the same
    order_id (sequential) reuse the same Lock object — important so a
    near-simultaneous third call still serializes against the prior."""
    handler = client.app.state.handler
    payload = make_alert(order_id="bookkeep_unique")
    handler.handle(payload)
    lock_first = handler._order_id_locks.get("bookkeep_unique")
    assert lock_first is not None

    # Same order_id again — duplicate-rejected but still serialized.
    handler.handle(payload)
    lock_second = handler._order_id_locks.get("bookkeep_unique")
    assert lock_second is lock_first
