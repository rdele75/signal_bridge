"""Tests for finding M2 — surface ENABLE_KILL_SWITCH=false.

Setting ``ENABLE_KILL_SWITCH=false`` makes ``KillSwitch.is_active()``
return False unconditionally — the dashboard toggle no longer blocks
trades and the live kill-switch gate trivially passes. The audit
found this had no surfacing: no startup warning, no dashboard
indicator. This file pins down both touchpoints.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


def _build_app_with_kill_switch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    enabled: bool,
):
    monkeypatch.setenv("ENABLE_KILL_SWITCH", "true" if enabled else "false")
    monkeypatch.setenv("APP_HOST", "127.0.0.1")
    monkeypatch.setenv("EXECUTION_MODE", "paper")
    monkeypatch.setenv("BROKER_PROVIDER", "paper")
    monkeypatch.setenv("BROKER", "paper")
    monkeypatch.setenv("TRADINGVIEW_WEBHOOK_SECRET", "x" * 32)
    monkeypatch.setenv("ALLOWED_SYMBOLS", "MES1!,MNQ1!")
    monkeypatch.setenv("ADMIN_AUTH_ENABLED", "false")
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "test-admin-password")
    monkeypatch.setenv("SESSION_SECRET", "test-session-secret-do-not-use-in-prod")
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "sb.db"))
    monkeypatch.setenv("LOG_PATH", str(tmp_path / "sb.log"))
    monkeypatch.setenv(
        "SYMBOLS_MAP_PATH", str(tmp_path / "missing_symbols.json")
    )

    for mod in [m for m in list(sys.modules) if m.startswith("app")]:
        del sys.modules[mod]
    from app import config as config_mod  # noqa: E402

    config_mod.get_settings.cache_clear()
    from app.main import create_app  # noqa: E402

    return create_app()


def test_startup_warning_when_kill_switch_disabled(
    tmp_path, monkeypatch, caplog
):
    with caplog.at_level(logging.WARNING, logger="signalbridge"):
        _build_app_with_kill_switch(tmp_path, monkeypatch, enabled=False)

    assert any(
        "ENABLE_KILL_SWITCH=false" in record.message
        and record.levelno == logging.WARNING
        for record in caplog.records
    )


def test_no_startup_warning_when_kill_switch_enabled(
    tmp_path, monkeypatch, caplog
):
    with caplog.at_level(logging.WARNING, logger="signalbridge"):
        _build_app_with_kill_switch(tmp_path, monkeypatch, enabled=True)

    warnings = [
        r for r in caplog.records
        if r.levelno == logging.WARNING
        and "ENABLE_KILL_SWITCH" in r.message
    ]
    assert warnings == [], f"unexpected kill-switch warning: {warnings!r}"


def test_dashboard_shows_disabled_config_badge(tmp_path, monkeypatch):
    """When ENABLE_KILL_SWITCH=false the dashboard's exec-card row
    swaps the active/off indicator for an explicit
    "disabled (config)" pill carrying the badge-warn class so it
    visually flags the config gap."""
    app = _build_app_with_kill_switch(tmp_path, monkeypatch, enabled=False)
    with TestClient(app) as c:
        body = c.get("/").text

    assert "kill switch disabled (config)" in body
    # The indicator carries the warn-style class so CSS picks it up.
    assert "badge-warn" in body
    # Sanity: the legacy "kill switch off" pill is NOT rendered when
    # disabled — those words should not appear in a static label.
    assert ">kill switch off<" not in body
    assert ">kill switch active<" not in body


def test_dashboard_shows_normal_pill_when_kill_switch_enabled(
    tmp_path, monkeypatch
):
    app = _build_app_with_kill_switch(tmp_path, monkeypatch, enabled=True)
    with TestClient(app) as c:
        body = c.get("/").text

    # The new "disabled (config)" copy must NOT appear when the switch
    # is enabled.
    assert "kill switch disabled (config)" not in body
    # The default "off" pill is what shows on a freshly booted, idle
    # paper config.
    assert "kill switch off" in body
