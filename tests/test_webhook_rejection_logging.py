"""Tests for the diagnostic rejection logger.

When a webhook is rejected, the log line must include a redacted +
truncated preview of the payload so the operator can debug bad
alerts. Sensitive values (secret/token/api_key/password/auth) must
NEVER appear in the log — those are the entire reason the operator
is debugging here, and leaking them through logs would be a security
regression.
"""
from __future__ import annotations

import json
import logging

from fastapi.testclient import TestClient

from app.webhook import (
    _PAYLOAD_PREVIEW_MAX_CHARS,
    _REDACT_KEYS,
    _redact_payload_for_log,
)

from .conftest import SECRET


# ----------------------------------------------------------------------
# _redact_payload_for_log — unit tests
# ----------------------------------------------------------------------


def test_redact_known_sensitive_keys():
    """The ``secret`` value is replaced with ``<redacted>``. Other
    fields are preserved so the operator can still see what was sent."""
    out = _redact_payload_for_log(
        {"secret": "abc123", "symbol": "MES1!", "action": "buy"}
    )
    # Sensitive value masked.
    assert '"secret": "<redacted>"' in out
    assert "abc123" not in out
    # Non-sensitive keys preserved verbatim.
    assert "MES1!" in out
    assert "buy" in out


def test_redact_is_case_insensitive():
    """Mixed-case key names must still be caught — TradingView and
    hand-rolled clients aren't consistent about casing."""
    out = _redact_payload_for_log({
        "Secret": "aa",
        "API_KEY": "bb",
        "Token": "cc",
        "Password": "dd",
        "auth": "ee",
    })
    for raw_value in ("aa", "bb", "cc", "dd", "ee"):
        assert raw_value not in out, (raw_value, out)
    assert out.count("<redacted>") == 5


def test_redact_covers_every_documented_key():
    """Every key listed in _REDACT_KEYS must be redacted. This pins
    the implementation against the documentation in the module."""
    payload = {key: f"value_for_{key}" for key in _REDACT_KEYS}
    out = _redact_payload_for_log(payload)
    for key in _REDACT_KEYS:
        assert f"value_for_{key}" not in out, (key, out)


def test_truncate_long_preview():
    """Payloads with multi-hundred-char fields must be cut to the
    configured cap and marked with ``...(truncated)``."""
    long_value = "x" * 1000
    out = _redact_payload_for_log(
        {"symbol": "MES1!", "blob": long_value, "action": "buy"}
    )
    assert out.endswith("...(truncated)"), out[-50:]
    # The preview can be at most cap + truncation marker. 200 chars
    # + the literal "...(truncated)" (14 chars) = 214.
    assert len(out) <= _PAYLOAD_PREVIEW_MAX_CHARS + len("...(truncated)")


def test_non_dict_payloads_are_handled_safely():
    """A non-dict payload (string, list, None) must not raise. The
    handler stringifies it and applies the same truncation rule."""
    assert _redact_payload_for_log("plain string body") == (
        "'plain string body'"
    )
    list_preview = _redact_payload_for_log([1, 2, 3])
    assert "1" in list_preview and "2" in list_preview
    assert _redact_payload_for_log(None) == "None"
    # Very long string is truncated.
    long_str = "x" * 1000
    out = _redact_payload_for_log(long_str)
    assert out.endswith("...(truncated)")


def test_nested_dict_secret_is_redacted():
    """A ``secret`` nested one level deep (e.g. ``order.secret``)
    must also be masked. We do a shallow walk because TradingView
    payloads aren't deeply nested in practice."""
    out = _redact_payload_for_log({
        "order": {"secret": "abc", "symbol": "MES1!"},
        "extra": "fine",
    })
    assert "abc" not in out, out
    assert "MES1!" in out
    assert "fine" in out
    assert "<redacted>" in out


def test_redact_handles_non_serialisable_values():
    """A value that ``json.dumps`` can't render (e.g. an arbitrary
    object) must not crash the redactor — it falls back to ``repr``."""
    class _Opaque:
        def __repr__(self):
            return "<opaque>"

    out = _redact_payload_for_log({"symbol": "MES1!", "weird": _Opaque()})
    # No exception, value rendered through default=str / repr.
    assert "MES1!" in out


# ----------------------------------------------------------------------
# End-to-end: rejection log line shape via the webhook handler
# ----------------------------------------------------------------------


def test_rejection_log_line_includes_payload_preview(client, caplog):
    """A real malformed_payload rejection must produce a log line
    matching ``REJECTED reason=... payload=...`` and the configured
    secret must NOT appear anywhere in the captured logs."""
    # Send a non-dict (a bare JSON list) — handle() catches this in
    # the very first isinstance check and rejects as malformed_payload.
    with caplog.at_level(logging.INFO, logger="signalbridge.webhook"):
        r = client.post("/webhooks/tradingview", json=[1, 2, 3])
    assert r.status_code == 200
    assert r.json()["rejection_reason"] == "malformed_payload"
    log_text = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "REJECTED reason=malformed_payload" in log_text
    assert "payload=" in log_text
    # Per spec: the operator's webhook secret must NEVER appear in
    # the captured log surface — even when it wasn't actually in the
    # rejected payload, this assertion guards against future
    # regressions that leak it via other log lines.
    assert SECRET not in log_text


def test_rejection_log_line_redacts_inline_secret(client, caplog):
    """If the operator sends a payload that DOES carry their secret
    but is otherwise malformed (missing required fields), the
    rejection log must redact the secret value."""
    bad = {"secret": SECRET, "ticker": "MES1!"}  # missing required
    with caplog.at_level(logging.INFO, logger="signalbridge.webhook"):
        r = client.post("/webhooks/tradingview", json=bad)
    assert r.status_code == 200
    log_text = "\n".join(rec.getMessage() for rec in caplog.records)
    # The REJECTED line must redact the secret value...
    rejected_lines = [
        line for line in log_text.splitlines() if "REJECTED" in line
    ]
    assert rejected_lines, log_text
    for line in rejected_lines:
        assert SECRET not in line, line
        assert "<redacted>" in line, line


def test_rejection_log_payload_is_truncated_for_huge_payload(
    client, caplog,
):
    """Real-world TradingView alerts can be much larger than 200 chars.
    The preview must still be capped + truncation-marked so the log
    file doesn't accumulate megabytes of unbounded alert content."""
    big = {"symbol": "MES1!", "comment": "z" * 4000}
    with caplog.at_level(logging.INFO, logger="signalbridge.webhook"):
        r = client.post("/webhooks/tradingview", json=big)
    assert r.status_code == 200
    log_text = "\n".join(rec.getMessage() for rec in caplog.records)
    rejected_lines = [
        line for line in log_text.splitlines() if "REJECTED" in line
    ]
    assert rejected_lines
    for line in rejected_lines:
        assert "...(truncated)" in line, line


def test_redact_payload_is_single_line():
    """JSON output must be a single line — multi-line log entries
    break grep / structured-log parsers."""
    out = _redact_payload_for_log({"a": "1\n2", "b": "ok"})
    # No literal newline characters in the output; \n inside strings
    # is fine (json.dumps escapes them).
    assert "\n" not in out
