"""Tests for finding H3 — max_open_positions counts broker positions.

Previously the cap only counted positions the bot itself opened (paper
broker writes; Topstep never wrote to the journal). An operator who
manually opened positions in TopstepX could blow past the cap without
the gate firing. The risk engine now merges broker.get_positions() into
the journal's open-position set so the count reflects real exposure.
"""
from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.journal import Journal
from app.kill_switch import KillSwitch
from app.risk_engine import RiskEngine
from app.schemas import NormalizedSignal


def _settings_with_cap(monkeypatch, cap: int):
    """Build a Settings instance with a specific max_open_positions cap
    and a permissive allow-list for the test symbols."""
    monkeypatch.setenv("ALLOWED_SYMBOLS", "MES1!,MNQ1!,ES1!")
    monkeypatch.setenv("MAX_OPEN_POSITIONS", str(cap))
    monkeypatch.setenv("MAX_CONTRACTS_PER_TRADE", "5")
    monkeypatch.setenv("TRADINGVIEW_WEBHOOK_SECRET", "x" * 32)
    import sys

    for mod in [m for m in list(sys.modules) if m.startswith("app")]:
        del sys.modules[mod]
    from app import config as config_mod  # noqa: E402

    config_mod.get_settings.cache_clear()
    return config_mod.get_settings()


def _entry_signal(symbol: str) -> NormalizedSignal:
    return NormalizedSignal(
        source="tradingview",
        strategy="t",
        symbol=symbol,
        broker_symbol=symbol,
        exchange=None,
        action="BUY",
        contracts=1,
        price=5000.0,
        order_id=f"h3_{symbol}",
        comment=None,
        timeframe=None,
        raw={},
    )


def _stub_topstep(positions):
    """Return a MagicMock that quacks like a TopstepBroker for the
    risk engine's purposes — only ``provider`` and ``get_positions``
    are exercised."""
    broker = MagicMock()
    broker.provider = "topstep"
    broker.get_positions.return_value = {
        "ok": True,
        "provider": "topstep",
        "positions": list(positions),
    }
    return broker


def test_journal_only_count_unchanged_on_paper(tmp_path: Path, monkeypatch):
    """Paper broker doesn't trigger the broker merge — behavior is the
    legacy journal-only count."""
    settings = _settings_with_cap(monkeypatch, cap=1)
    j = Journal(tmp_path / "j.db")
    ks = KillSwitch(tmp_path / "kill.active", enabled=False)
    paper_broker = MagicMock()
    paper_broker.provider = "paper"
    risk = RiskEngine(settings, j, ks, broker=paper_broker)

    # Journal has one open position on MES1! — the second-symbol entry
    # would push to 2 open, above the cap.
    j.upsert_position(symbol="MES1!", quantity=1, avg_price=5000.0, side="long")
    decision = risk.evaluate(_entry_signal("MNQ1!"))
    assert decision.accepted is False
    assert "max_open_positions_reached (1/1)" in decision.reason
    # Paper broker.get_positions is NOT consulted (provider != topstep).
    paper_broker.get_positions.assert_not_called()


def test_topstep_position_counts_toward_cap(tmp_path: Path, monkeypatch):
    """A Topstep position the journal doesn't know about must count."""
    settings = _settings_with_cap(monkeypatch, cap=1)
    j = Journal(tmp_path / "j.db")
    ks = KillSwitch(tmp_path / "kill.active", enabled=False)
    broker = _stub_topstep(
        [
            {"symbol": "MES1!", "contractId": "CON.F.US.MES.M26", "size": 2},
        ]
    )
    risk = RiskEngine(settings, j, ks, broker=broker)

    # Journal is empty. Broker says MES1! has 2 contracts open. A new
    # entry on a DIFFERENT symbol must be refused — cap is 1.
    decision = risk.evaluate(_entry_signal("MNQ1!"))
    assert decision.accepted is False
    assert "max_open_positions_reached" in decision.reason
    broker.get_positions.assert_called_once()


def test_topstep_same_symbol_entry_passes_when_at_cap(tmp_path, monkeypatch):
    """Adding to an existing position should NOT be blocked by the cap."""
    settings = _settings_with_cap(monkeypatch, cap=1)
    j = Journal(tmp_path / "j.db")
    ks = KillSwitch(tmp_path / "kill.active", enabled=False)
    broker = _stub_topstep([{"symbol": "MES1!", "size": 1}])
    risk = RiskEngine(settings, j, ks, broker=broker)

    decision = risk.evaluate(_entry_signal("MES1!"))
    assert decision.accepted is True, decision.reason


def test_broker_get_positions_failure_falls_back_to_journal(
    tmp_path: Path, monkeypatch, caplog
):
    """Network/adapter exception during get_positions falls back to
    journal-only count and logs WARNING."""
    settings = _settings_with_cap(monkeypatch, cap=2)
    j = Journal(tmp_path / "j.db")
    ks = KillSwitch(tmp_path / "kill.active", enabled=False)
    broker = MagicMock()
    broker.provider = "topstep"
    broker.get_positions.side_effect = RuntimeError("simulated network error")
    risk = RiskEngine(settings, j, ks, broker=broker)

    j.upsert_position(symbol="MES1!", quantity=1, avg_price=5000.0, side="long")
    with caplog.at_level(logging.WARNING, logger="signalbridge.risk_engine"):
        decision = risk.evaluate(_entry_signal("MNQ1!"))

    # journal-only count = 1, cap = 2 → allowed.
    assert decision.accepted is True
    # WARNING was emitted.
    assert any(
        "broker.get_positions() failed" in record.message
        and record.levelno == logging.WARNING
        for record in caplog.records
    )


def test_broker_envelope_not_ok_does_not_count(tmp_path: Path, monkeypatch):
    """Topstep returning ok=false (e.g. missing_credentials) is not an
    error — the merge silently skips and the journal count is used."""
    settings = _settings_with_cap(monkeypatch, cap=1)
    j = Journal(tmp_path / "j.db")
    ks = KillSwitch(tmp_path / "kill.active", enabled=False)
    broker = MagicMock()
    broker.provider = "topstep"
    broker.get_positions.return_value = {
        "ok": False,
        "status": "missing_credentials",
        "provider": "topstep",
        "positions": [],
    }
    risk = RiskEngine(settings, j, ks, broker=broker)

    decision = risk.evaluate(_entry_signal("MES1!"))
    assert decision.accepted is True
