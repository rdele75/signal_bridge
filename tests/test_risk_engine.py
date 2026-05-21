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


def test_kill_switch_blocks_only_when_armed(tmp_path):
    """Post-collapse: kill switch is consulted only when execution is
    Armed. Off and Test states ignore it so the operator can verify
    plumbing while the switch is hot."""
    risk, _, ks, settings = _build(tmp_path)
    ks.activate("manual halt")

    settings.execution_mode = "off"
    assert risk.evaluate(_signal()).accepted is True

    settings.execution_mode = "test"
    assert risk.evaluate(_signal()).accepted is True

    settings.execution_mode = "armed"
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
    """MAX_DAILY_LOSS is enforced in DOLLARS — the engine converts
    today's closed-trade points P&L per instrument before comparing."""
    risk, journal, _, _ = _build(tmp_path, max_daily_loss=100.0)
    # ES1! @ $12.50/pt × -10 pts = -$125 → trips the $100 cap.
    journal.record_closed_trade(
        symbol="ES1!", side="long", contracts=1,
        entry_price=5000.0, exit_price=4990.0,
        realized_pnl_points=-10.0,
    )
    d = risk.evaluate(_signal())
    assert d.accepted is False
    assert d.reason == "daily_loss_limit_reached"


def test_daily_loss_limit_uses_dollars_not_points(tmp_path):
    """A -50 points loss on MNQ1! ($0.50/pt = -$25) must NOT trip a
    $100 daily-loss cap. The pre-merge code compared points-to-points,
    which silently fired too early on the small contract."""
    risk, journal, _, _ = _build(tmp_path, max_daily_loss=100.0)
    journal.record_closed_trade(
        symbol="MNQ1!", side="long", contracts=1,
        entry_price=20000.0, exit_price=19950.0,
        realized_pnl_points=-50.0,
    )
    d = risk.evaluate(_signal(symbol="MES1!", order_id="post_loss_1"))
    assert d.accepted is True, d


def test_points_to_dollars_known_and_unknown_symbols():
    """``points_to_dollars`` returns the symbol's $ value × points for
    known instruments and 0.0 for unknown — the caller is expected to
    handle the unknown branch (typically with a WARNING log)."""
    from app.risk_engine import INSTRUMENT_POINT_VALUES_USD, points_to_dollars

    assert "ES1!" in INSTRUMENT_POINT_VALUES_USD
    assert points_to_dollars("ES1!", -10.0) == -125.0
    assert points_to_dollars("MES1!", 4.0) == 5.0
    assert points_to_dollars("MNQ1!", 100.0) == 50.0
    # Unknown symbols contribute 0.0.
    assert points_to_dollars("WHEAT1!", -5.0) == 0.0


def test_get_daily_pnl_dollars_sums_per_instrument(tmp_path):
    """``Journal.get_daily_pnl_dollars`` multiplies each closed trade's
    points by its instrument's dollar value and sums."""
    journal = Journal(tmp_path / "pnl.db")
    journal.record_closed_trade(
        symbol="MES1!", side="long", contracts=1,
        entry_price=5000.0, exit_price=4990.0,
        realized_pnl_points=-10.0,  # -$12.50
    )
    journal.record_closed_trade(
        symbol="ES1!", side="short", contracts=2,
        entry_price=5050.0, exit_price=5045.0,
        realized_pnl_points=5.0,  # +$62.50 (12.50 * 5)
    )
    # MNQ1! at +20 pts = $10
    journal.record_closed_trade(
        symbol="MNQ1!", side="long", contracts=1,
        entry_price=20000.0, exit_price=20020.0,
        realized_pnl_points=20.0,
    )
    pnl = journal.get_daily_pnl_dollars()
    # -12.50 + 62.50 + 10.00 = 60.00
    assert pnl == pytest.approx(60.0)


def test_max_daily_loss_unit_migration_resets_legacy_db(tmp_path, caplog):
    """A legacy DB carrying a points-semantic MAX_DAILY_LOSS gets reset
    to 0.0 on boot with a CRITICAL log — the operator must re-enter
    the cap in dollars before the gate re-engages."""
    import logging
    from app.settings_store import (
        MAX_DAILY_LOSS_UNIT_VERSION_CURRENT,
        SettingsStore,
        migrate_max_daily_loss_units,
    )

    store = SettingsStore(tmp_path / "legacy.db")
    store.set_setting("MAX_DAILY_LOSS", "250")  # legacy points value
    settings = Settings(max_daily_loss=250.0)

    caplog.set_level(logging.CRITICAL, logger="signalbridge.settings_store")
    reset = migrate_max_daily_loss_units(
        store, settings,
        logging.getLogger("signalbridge.settings_store"),
    )

    assert reset is True
    assert settings.max_daily_loss == 0.0
    assert store.get_setting("MAX_DAILY_LOSS") == "0.0"
    assert (
        store.get_setting("MAX_DAILY_LOSS_UNIT_VERSION")
        == str(MAX_DAILY_LOSS_UNIT_VERSION_CURRENT)
    )
    # CRITICAL log must contain the per-instrument cheat sheet so the
    # operator knows what to re-enter.
    critical_messages = [r.message for r in caplog.records if r.levelno >= logging.CRITICAL]
    assert any("DOLLARS" in m and "MES" in m for m in critical_messages), critical_messages


def test_max_daily_loss_unit_migration_fresh_install_just_stamps(tmp_path):
    """A fresh DB (no MAX_DAILY_LOSS row yet) stamps the version
    without firing a reset — first boot should not look like an
    upgrade."""
    from app.settings_store import (
        MAX_DAILY_LOSS_UNIT_VERSION_CURRENT,
        SettingsStore,
        migrate_max_daily_loss_units,
    )

    store = SettingsStore(tmp_path / "fresh.db")
    settings = Settings(max_daily_loss=0.0)

    reset = migrate_max_daily_loss_units(store, settings)

    assert reset is False
    assert settings.max_daily_loss == 0.0
    assert (
        store.get_setting("MAX_DAILY_LOSS_UNIT_VERSION")
        == str(MAX_DAILY_LOSS_UNIT_VERSION_CURRENT)
    )


def test_max_daily_loss_unit_migration_idempotent(tmp_path):
    """Once stamped, the migration is a no-op — re-running it does not
    touch an already-dollarized MAX_DAILY_LOSS."""
    from app.settings_store import (
        MAX_DAILY_LOSS_UNIT_VERSION_CURRENT,
        SettingsStore,
        migrate_max_daily_loss_units,
    )

    store = SettingsStore(tmp_path / "stamped.db")
    store.set_setting("MAX_DAILY_LOSS", "1500.0")  # already in dollars
    store.set_internal_setting(
        "MAX_DAILY_LOSS_UNIT_VERSION",
        str(MAX_DAILY_LOSS_UNIT_VERSION_CURRENT),
    )
    settings = Settings(max_daily_loss=1500.0)

    reset = migrate_max_daily_loss_units(store, settings)

    assert reset is False
    assert store.get_setting("MAX_DAILY_LOSS") == "1500.0"
    assert settings.max_daily_loss == 1500.0
