"""Unit tests for the RiskEngine — exercised without FastAPI."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.config import Settings
from app.journal import Journal
from app.kill_switch import KillSwitch
from app.risk_engine import RiskEngine, normalize_action
from app.schemas import NormalizedSignal


def _build(tmp_path: Path, **overrides) -> tuple[RiskEngine, Journal, KillSwitch, Settings]:
    db = tmp_path / "rt.db"
    log = tmp_path / "rt.log"
    base = dict(
        app_name="SignalBridge",
        app_host="127.0.0.1",
        app_port=8000,
        execution_mode="paper",
        broker_provider="paper",
        broker="paper",
        webhook_secret="s",
        allowed_symbols=["MES1!", "MNQ1!"],
        max_contracts_per_trade=1,
        max_daily_loss=250.0,
        max_open_positions=1,
        enable_longs=True,
        enable_shorts=True,
        enable_kill_switch=True,
        database_path=str(db),
        log_path=str(log),
        log_level="WARNING",
        duplicate_order_cooldown_seconds=60,
    )
    base.update(overrides)
    settings = Settings(**base)
    journal = Journal(settings.database_abs_path)
    ks = KillSwitch(
        settings.database_abs_path.parent / "kill_switch.active",
        enabled=settings.enable_kill_switch,
    )
    return RiskEngine(settings, journal, ks), journal, ks, settings


def _signal(**overrides) -> NormalizedSignal:
    base = dict(
        source="tradingview",
        strategy="t",
        symbol="MES1!",
        exchange="CME_MINI",
        action="BUY",
        contracts=1,
        price=5000.0,
        order_id="rt_001",
        comment=None,
        raw={},
    )
    base.update(overrides)
    return NormalizedSignal(**base)


def test_normalize_action_table():
    assert normalize_action("buy") == "BUY"
    assert normalize_action("BUY") == "BUY"
    assert normalize_action("long") == "BUY"
    assert normalize_action("sell") == "SELL"
    assert normalize_action("short") == "SHORT"
    assert normalize_action("cover") == "COVER"
    assert normalize_action("exit") == "EXIT"
    assert normalize_action("close") == "EXIT"
    assert normalize_action("nonsense") is None
    assert normalize_action("") is None


def test_allowed_symbol_passes(tmp_path):
    risk, *_ = _build(tmp_path)
    d = risk.evaluate(_signal())
    assert d.accepted is True


def test_unknown_symbol_rejected(tmp_path):
    risk, *_ = _build(tmp_path)
    d = risk.evaluate(_signal(symbol="AAPL"))
    assert d.accepted is False
    assert "symbol_not_allowed" in d.reason


def test_contracts_cap_rejected(tmp_path):
    risk, *_ = _build(tmp_path, max_contracts_per_trade=1)
    d = risk.evaluate(_signal(contracts=5))
    assert d.accepted is False
    assert "contracts_above_max" in d.reason


def test_invalid_contracts_rejected(tmp_path):
    risk, *_ = _build(tmp_path)
    d = risk.evaluate(_signal(contracts=0))
    assert d.accepted is False
    assert d.reason == "invalid_contracts"


def test_longs_disabled(tmp_path):
    risk, *_ = _build(tmp_path, enable_longs=False)
    d = risk.evaluate(_signal(action="BUY"))
    assert d.accepted is False
    assert d.reason == "longs_disabled"


def test_shorts_disabled(tmp_path):
    risk, *_ = _build(tmp_path, enable_shorts=False)
    d = risk.evaluate(_signal(action="SHORT"))
    assert d.accepted is False
    assert d.reason == "shorts_disabled"

    d2 = risk.evaluate(_signal(action="SELL"))
    assert d2.accepted is False
    assert d2.reason == "shorts_disabled"


def test_kill_switch_blocks(tmp_path):
    risk, _, ks, _ = _build(tmp_path)
    ks.activate("manual halt")
    d = risk.evaluate(_signal())
    assert d.accepted is False
    assert d.reason == "kill_switch_active"


def test_duplicate_order_id_rejected(tmp_path):
    risk, journal, _, _ = _build(tmp_path)
    journal.record_signal(
        source="tradingview",
        strategy="t",
        symbol="MES1!",
        action="BUY",
        contracts=1,
        price=5000.0,
        order_id="dup_42",
        raw_payload={},
        decision="accepted",
        rejection_reason=None,
        execution_mode="paper",
        execution_result={"ok": True},
    )
    d = risk.evaluate(_signal(order_id="dup_42"))
    assert d.accepted is False
    assert d.reason == "duplicate_order_id"


def test_max_open_positions(tmp_path):
    risk, journal, _, _ = _build(tmp_path, max_open_positions=1)
    # Pretend we already have a position in MNQ1!.
    journal.upsert_position(symbol="MNQ1!", quantity=1, avg_price=20000.0, side="long")
    d = risk.evaluate(_signal(symbol="MES1!", order_id="entry_2"))
    assert d.accepted is False
    assert "max_open_positions_reached" in d.reason


def test_daily_loss_limit(tmp_path):
    risk, journal, _, _ = _build(tmp_path, max_daily_loss=100.0)
    journal.add_daily_pnl(-150.0)
    d = risk.evaluate(_signal())
    assert d.accepted is False
    assert d.reason == "daily_loss_limit_reached"
