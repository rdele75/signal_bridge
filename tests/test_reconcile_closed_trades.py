"""Tests for the reactive (D2) close-trade reconciliation path.

These cover the deliverables in
``closed-trade-reconciliation-2026-05-21``:

* ``find_open_entry_for_symbol`` returns the oldest unmatched entry
  signal and ``None`` when every entry has already been closed.
* A fill discovered via ``/api/Order/search`` is paired FIFO with an
  open entry and recorded as a ``closed_trades`` row with computed
  dollar P&L.
* The dedupe guard (``closed_trade_exists_for_order_id``) prevents
  the reactive and periodic paths from double-recording the same
  Topstep order.
"""
from __future__ import annotations

import json
from pathlib import Path

from app.execution.topstep import TopstepBroker
from app.journal import Journal
from app.schemas import NormalizedSignal


def _make_signal(action: str = "EXIT", **overrides) -> NormalizedSignal:
    base = dict(
        source="tradingview",
        strategy="orb",
        symbol="MES1!",
        broker_symbol="CON.F.US.MES.M26",
        exchange="CME_MINI",
        action=action,
        contracts=1,
        price=5002.50,
        order_id="reconcile_test_1",
        comment="reconcile unit test",
        timeframe="1",
        raw={},
    )
    base.update(overrides)
    return NormalizedSignal(**base)


def _make_broker(journal: Journal) -> TopstepBroker:
    return TopstepBroker(
        username="u",
        api_key="k",
        account_id="5001",
        token="JWT",
        token_expires_at="2099-01-01T00:00:00+00:00",
        execution_mode="armed",
        allowed_symbols=["MES1!"],
        max_contracts_per_trade=1,
        kill_switch_enabled=False,
        journal=journal,
    )


def _seed_entry(journal: Journal, *, symbol: str = "MES1!",
                action: str = "BUY", price: float = 5000.0,
                contracts: int = 1) -> int:
    """Drop a representative accepted entry signal in the journal."""
    return journal.record_signal(
        source="tradingview",
        strategy="orb",
        symbol=symbol,
        action=action,
        contracts=contracts,
        price=price,
        order_id=None,
        raw_payload={},
        decision="accepted",
        rejection_reason=None,
        execution_mode="armed",
        broker_provider="topstep",
    )


# ---------------------------------------------------------------------------
# find_open_entry_for_symbol
# ---------------------------------------------------------------------------


def test_find_open_entry_returns_oldest_unmatched(tmp_path: Path):
    j = Journal(tmp_path / "j.db")
    _seed_entry(j, action="BUY", price=5000.0)
    _seed_entry(j, action="BUY", price=5010.0)
    e = j.find_open_entry_for_symbol("MES1!")
    assert e is not None
    assert e["price"] == 5000.0  # oldest first


def test_find_open_entry_skips_consumed_entries(tmp_path: Path):
    """One closed_trade consumes one entry — the next call should
    return the second entry."""
    j = Journal(tmp_path / "j.db")
    _seed_entry(j, action="BUY", price=5000.0)
    _seed_entry(j, action="BUY", price=5010.0)
    j.record_closed_trade(
        symbol="MES1!", side="long", contracts=1,
        entry_price=5000.0, exit_price=5002.0,
        realized_pnl_points=2.0, broker_provider="topstep",
        topstep_order_id="9001",
    )
    e = j.find_open_entry_for_symbol("MES1!")
    assert e is not None
    assert e["price"] == 5010.0


def test_find_open_entry_returns_none_when_balanced(tmp_path: Path):
    j = Journal(tmp_path / "j.db")
    _seed_entry(j, action="BUY", price=5000.0)
    j.record_closed_trade(
        symbol="MES1!", side="long", contracts=1,
        entry_price=5000.0, exit_price=5002.0,
        realized_pnl_points=2.0, broker_provider="topstep",
        topstep_order_id="9001",
    )
    assert j.find_open_entry_for_symbol("MES1!") is None


def test_find_open_entry_returns_none_for_unknown_symbol(tmp_path: Path):
    j = Journal(tmp_path / "j.db")
    assert j.find_open_entry_for_symbol("NQ1!") is None


# ---------------------------------------------------------------------------
# closed_trade_exists_for_order_id
# ---------------------------------------------------------------------------


def test_closed_trade_exists_for_order_id_roundtrip(tmp_path: Path):
    j = Journal(tmp_path / "j.db")
    assert j.closed_trade_exists_for_order_id("9001") is False
    j.record_closed_trade(
        symbol="MES1!", side="long", contracts=1,
        entry_price=5000.0, exit_price=5002.0,
        realized_pnl_points=2.0,
        broker_provider="topstep", topstep_order_id="9001",
    )
    assert j.closed_trade_exists_for_order_id("9001") is True
    # An empty id never matches (legacy paper closes have no broker id).
    assert j.closed_trade_exists_for_order_id("") is False


# ---------------------------------------------------------------------------
# _record_reconciled_close (the heart of the reactive + periodic path)
# ---------------------------------------------------------------------------


def test_record_reconciled_close_writes_dollar_pnl(tmp_path: Path):
    """A long entry @5000 + exit fill @5002 + MES1! ($1.25/pt) =
    +$2.50 realized dollars. The row also carries the Topstep
    order id so the periodic poll won't double-record it."""
    j = Journal(tmp_path / "j.db")
    _seed_entry(j, action="BUY", price=5000.0)
    broker = _make_broker(j)
    fill = {
        "orderId": "9001",
        "filledPrice": 5002.0,
        "size": 1,
        "side": 1,  # SELL — closes the BUY
    }
    broker._record_reconciled_close(
        signal=_make_signal(action="EXIT"),
        broker_order_id="9001",
        fill=fill,
        source="reactive_test",
    )
    rows = j.list_recent_closed_trades(limit=5)
    assert len(rows) == 1
    row = rows[0]
    assert row["symbol"] == "MES1!"
    assert row["side"] == "long"
    assert row["entry_price"] == 5000.0
    assert row["exit_price"] == 5002.0
    assert row["realized_pnl_points"] == 2.0
    assert row["realized_pnl_dollars"] == 2.5
    assert row["topstep_order_id"] == "9001"
    assert j.closed_trade_exists_for_order_id("9001") is True


def test_record_reconciled_close_short_side(tmp_path: Path):
    """A short entry @5010 closed at 5000 yields +10 pts × $1.25 = $12.50."""
    j = Journal(tmp_path / "j.db")
    _seed_entry(j, action="SHORT", price=5010.0)
    broker = _make_broker(j)
    fill = {"orderId": "9002", "filledPrice": 5000.0, "size": 1, "side": 0}
    broker._record_reconciled_close(
        signal=_make_signal(action="COVER"),
        broker_order_id="9002",
        fill=fill,
        source="reactive_test",
    )
    row = j.list_recent_closed_trades(limit=1)[0]
    assert row["side"] == "short"
    assert row["realized_pnl_points"] == 10.0
    assert row["realized_pnl_dollars"] == 12.5


def test_record_reconciled_close_skips_when_no_open_entry(tmp_path: Path):
    """An EXIT fill that doesn't match any prior entry (operator
    opened the position directly in TopstepX) leaves the journal
    untouched. The periodic poll's WARNING flags it."""
    j = Journal(tmp_path / "j.db")
    broker = _make_broker(j)
    fill = {"orderId": "9003", "filledPrice": 5005.0, "size": 1, "side": 1}
    broker._record_reconciled_close(
        signal=_make_signal(action="EXIT"),
        broker_order_id="9003",
        fill=fill,
        source="reactive_test",
    )
    assert j.list_recent_closed_trades(limit=5) == []


def test_record_reconciled_close_unknown_symbol_logs_null_dollars(
    tmp_path: Path, caplog
):
    """A symbol not in ``INSTRUMENT_POINT_VALUES_USD`` records the row
    with ``realized_pnl_dollars=NULL`` and emits a WARNING the
    operator can grep for."""
    import logging

    j = Journal(tmp_path / "j.db")
    _seed_entry(j, symbol="WHEAT1!", action="BUY", price=600.0)
    broker = _make_broker(j)
    fill = {"orderId": "9004", "filledPrice": 605.0, "size": 1, "side": 1}
    with caplog.at_level(logging.WARNING, logger="signalbridge.broker.topstep"):
        broker._record_reconciled_close(
            signal=_make_signal(symbol="WHEAT1!", action="EXIT"),
            broker_order_id="9004",
            fill=fill,
            source="reactive_test",
        )
    row = j.list_recent_closed_trades(limit=1)[0]
    assert row["symbol"] == "WHEAT1!"
    assert row["realized_pnl_points"] == 5.0
    assert row["realized_pnl_dollars"] is None
    assert any(
        "no_multiplier" in r.message for r in caplog.records
    ), caplog.records


# ---------------------------------------------------------------------------
# Spawn gating: only EXIT/COVER triggers the daemon thread
# ---------------------------------------------------------------------------


def test_spawn_fill_reconcile_thread_skips_buy(tmp_path: Path):
    j = Journal(tmp_path / "j.db")
    broker = _make_broker(j)
    t = broker._spawn_fill_reconcile_thread(_make_signal(action="BUY"), "9001")
    assert t is None


def test_spawn_fill_reconcile_thread_skips_when_journal_missing(tmp_path: Path):
    broker = TopstepBroker(
        username="u", api_key="k", account_id="5001",
        token="JWT", token_expires_at="2099-01-01T00:00:00+00:00",
        execution_mode="armed", journal=None,
    )
    t = broker._spawn_fill_reconcile_thread(_make_signal(action="EXIT"), "9001")
    assert t is None


def test_spawn_fill_reconcile_thread_spawns_for_exit(tmp_path: Path,
                                                     monkeypatch):
    """An EXIT signal kicks off the reconcile daemon. We stub the body
    so the thread completes synchronously and we can assert it ran."""
    j = Journal(tmp_path / "j.db")
    broker = _make_broker(j)
    ran: list[str] = []

    def fake_reconcile(self, signal, broker_order_id):  # noqa: ARG001
        ran.append(broker_order_id)

    monkeypatch.setattr(
        TopstepBroker,
        "_reconcile_fill_after_submit",
        fake_reconcile,
    )
    t = broker._spawn_fill_reconcile_thread(
        _make_signal(action="EXIT"), "9999"
    )
    assert t is not None
    t.join(timeout=2.0)
    assert ran == ["9999"]


# ---------------------------------------------------------------------------
# End-to-end: armed EXIT submit triggers reconcile and writes the row.
# ---------------------------------------------------------------------------


def _post_factory_with_fill(filled_order_id: int, filled_price: float):
    """Auth + place + search responses for a successful close round-trip."""

    def _fake_post(self, path, payload, *, auth: bool = False):  # noqa: ARG001
        if path == "/api/Auth/loginKey":
            return 200, {
                "success": True, "token": "JWT.TOKEN",
                "errorCode": 0, "errorMessage": None,
            }
        if path == "/api/Account/search":
            return 200, {
                "success": True, "errorCode": 0, "errorMessage": None,
                "accounts": [{
                    "id": 5001, "name": "PA-50K", "balance": 50000.0,
                    "canTrade": True, "isVisible": True,
                }],
            }
        if path == "/api/Order/place":
            return 200, {
                "success": True, "orderId": filled_order_id,
                "errorCode": 0, "errorMessage": None,
            }
        if path == "/api/Order/search":
            return 200, {
                "success": True, "errorCode": 0, "errorMessage": None,
                "orders": [{
                    "id": filled_order_id,
                    "accountId": 5001,
                    "contractId": "CON.F.US.MES.M26",
                    "status": 2,
                    "side": 1,
                    "size": 1,
                    "filledPrice": filled_price,
                    "creationTimestamp": "2026-05-21T15:00:00Z",
                }],
            }
        return 200, {"success": True, "errorCode": 0}

    return _fake_post


def test_armed_exit_round_trip_writes_closed_trade(tmp_path, monkeypatch):
    """End-to-end: an entry signal hits the journal, an EXIT submits
    armed, the reactive thread polls /api/Order/search, the fill pairs
    with the prior entry, and a ``closed_trades`` row appears with a
    computed dollar P&L."""
    from tests.conftest import _build_app
    from tests.test_execution import _write_topstep_symbol_map

    # Speed up the reconciliation thread — 2s -> 0s -- so the test
    # doesn't sleep.
    monkeypatch.setattr("app.execution.topstep.time.sleep", lambda _: None)

    _write_topstep_symbol_map(tmp_path)
    app = _build_app(tmp_path, monkeypatch)
    broker = app.state.broker
    broker.execution_mode = "armed"
    broker.token = "JWT"
    broker.token_expires_at = "2099-01-01T00:00:00+00:00"
    broker._can_trade_cache[str(broker.account_id)] = True
    if not broker.username:
        broker.username = "test_user@example.com"
    if not broker.api_key:
        broker.api_key = "test_api_key_abcd1234"

    # Seed an open SHORT @5010 in the journal so the COVER fill has
    # something to pair against. (EXIT is rejected by the order
    # builder today — COVER is the available close-side action; both
    # trigger reactive reconciliation per _CLOSING_ACTIONS.)
    j = app.state.journal
    _seed_entry(j, symbol="MES1!", action="SHORT", price=5010.0)

    monkeypatch.setattr(
        broker.__class__,
        "_post_json",
        _post_factory_with_fill(filled_order_id=9001, filled_price=5004.0),
    )

    cover_signal = NormalizedSignal(
        source="tradingview", strategy="orb", symbol="MES1!",
        broker_symbol="CON.F.US.MES.M26", exchange="CME_MINI",
        action="COVER", contracts=1, price=5004.0,
        order_id="exit_round_trip_1", comment="", timeframe="1", raw={},
    )

    result = broker.submit_market_order(
        cover_signal, symbol_map=app.state.symbol_map
    )
    assert result["ok"] is True, json.dumps(result, default=str, indent=2)
    assert result["submitted"] is True

    # Drain the daemon thread.
    for t in list(__import__("threading").enumerate()):
        if t.name.startswith("signalbridge-reconcile-"):
            t.join(timeout=2.0)

    rows = j.list_recent_closed_trades(limit=5)
    assert len(rows) == 1, rows
    row = rows[0]
    assert row["symbol"] == "MES1!"
    assert row["side"] == "short"
    assert row["entry_price"] == 5010.0
    assert row["exit_price"] == 5004.0
    # SHORT @ 5010, covered @ 5004 = +6 pts profit
    assert row["realized_pnl_points"] == 6.0
    # MES1! @ $1.25/pt × 6 pts × 1 contract = $7.50
    assert row["realized_pnl_dollars"] == 7.5
    assert row["topstep_order_id"] == "9001"


# ---------------------------------------------------------------------------
# Periodic reconciliation (D3)
# ---------------------------------------------------------------------------


def _make_armed_broker(journal: Journal) -> TopstepBroker:
    b = _make_broker(journal)
    b.execution_mode = "armed"
    return b


def _stub_history(broker: TopstepBroker, orders: list[dict],
                  monkeypatch) -> None:
    def _fake_history(self, *, start_timestamp=None, end_timestamp=None,
                      lookback_days=None, limit=None):  # noqa: ARG001
        return {"ok": True, "provider": "topstep", "orders": orders}
    monkeypatch.setattr(broker.__class__, "get_order_history", _fake_history)


def test_periodic_reconcile_skips_when_execution_off(tmp_path: Path):
    """No fills should be reconciled while execution is off — the
    operator hasn't armed; nothing should be opening or closing."""
    j = Journal(tmp_path / "j.db")
    broker = _make_broker(j)  # execution_mode defaults to armed in factory
    broker.execution_mode = "off"
    assert broker.periodic_reconcile_once() == 0


def test_periodic_reconcile_dedupes_against_existing_close(
    tmp_path: Path, monkeypatch
):
    """A fill the reactive path already recorded must not get a second
    closed_trades row from the periodic sweep."""
    j = Journal(tmp_path / "j.db")
    _seed_entry(j, symbol="MES1!", action="BUY", price=5000.0)
    j.record_closed_trade(
        symbol="MES1!", side="long", contracts=1,
        entry_price=5000.0, exit_price=5002.0,
        realized_pnl_points=2.0,
        broker_provider="topstep", topstep_order_id="9001",
    )
    broker = _make_armed_broker(j)
    _stub_history(broker, [{
        "orderId": "9001", "filledPrice": 5002.0, "size": 1, "side": 1,
        "contractId": "CON.F.US.MES.M26",
    }], monkeypatch)
    assert broker.periodic_reconcile_once() == 0
    assert len(j.list_recent_closed_trades(limit=5)) == 1


def test_periodic_reconcile_records_orphan_fill(
    tmp_path: Path, monkeypatch
):
    """A close that the reactive path missed (network blip, missed
    /api/Order/search lookup) gets picked up by the periodic poll
    and recorded with the correct dollar P&L."""
    j = Journal(tmp_path / "j.db")
    # Seed both an entry and the broker_symbol mapping by also writing
    # the entry signal carrying broker_symbol so _resolve_symbol_for_order
    # can map the contractId back to MES1!.
    j.record_signal(
        source="tradingview", strategy="orb", symbol="MES1!",
        action="BUY", contracts=1, price=5000.0, order_id=None,
        raw_payload={}, decision="accepted", rejection_reason=None,
        execution_mode="armed", broker_provider="topstep",
        broker_symbol="CON.F.US.MES.M26",
    )
    broker = _make_armed_broker(j)
    _stub_history(broker, [{
        "orderId": "9007", "filledPrice": 5005.0, "size": 1, "side": 1,
        "contractId": "CON.F.US.MES.M26",
    }], monkeypatch)
    recorded = broker.periodic_reconcile_once()
    assert recorded == 1
    rows = j.list_recent_closed_trades(limit=5)
    assert len(rows) == 1
    row = rows[0]
    assert row["topstep_order_id"] == "9007"
    assert row["symbol"] == "MES1!"
    assert row["entry_price"] == 5000.0
    assert row["exit_price"] == 5005.0
    assert row["realized_pnl_points"] == 5.0
    assert row["realized_pnl_dollars"] == 6.25  # 5 pts * $1.25


def test_periodic_reconcile_skips_unfilled_orders(
    tmp_path: Path, monkeypatch
):
    """A working order (no filledPrice yet) is not yet a fill and
    should not be recorded."""
    j = Journal(tmp_path / "j.db")
    _seed_entry(j, symbol="MES1!", action="BUY", price=5000.0)
    broker = _make_armed_broker(j)
    _stub_history(broker, [{
        "orderId": "9008", "filledPrice": None, "size": 1, "side": 1,
        "contractId": "CON.F.US.MES.M26",
    }], monkeypatch)
    assert broker.periodic_reconcile_once() == 0


def test_periodic_reconcile_skips_orphan_with_no_open_entry(
    tmp_path: Path, monkeypatch
):
    """A fill with no matching open entry — operator opened a
    position directly in TopstepX before SignalBridge knew about
    it — stays unrecorded (the periodic poll does not fabricate
    a synthetic entry)."""
    j = Journal(tmp_path / "j.db")
    broker = _make_armed_broker(j)
    _stub_history(broker, [{
        "orderId": "9009", "filledPrice": 5002.0, "size": 1, "side": 1,
        "contractId": "CON.F.US.MES.M26",
    }], monkeypatch)
    assert broker.periodic_reconcile_once() == 0


def test_start_periodic_reconciliation_is_idempotent(tmp_path: Path):
    j = Journal(tmp_path / "j.db")
    broker = _make_armed_broker(j)
    t1 = broker.start_periodic_reconciliation(interval_seconds=3600)
    t2 = broker.start_periodic_reconciliation(interval_seconds=3600)
    assert t1 is not None
    assert t2 is None
    broker.stop_periodic_reconciliation()
