"""Xiznit Universal ORB native payload tests.

Covers the two-alert setup the strategy expects:
* Entries & TP exits via ``{{strategy.order.alert_message}}``
* SL moves & force-closes via ``{{strategy.alert_message}}``

The body never carries our secret in the Xiznit shape, so the endpoint
must accept it via query string or header.
"""
from __future__ import annotations

from .conftest import SECRET


WEBHOOK = "/webhooks/tradingview"


def _last_signal_row(client):
    """Pull the most recent journal row out of /api/journal/recent."""
    body = client.get("/api/journal/recent?limit=1").json()
    signals = body["signals"]
    assert signals, "expected at least one journaled signal"
    return signals[0]


# ---------- Entries ----------------------------------------------------


def test_xiznit_entry_with_query_secret_accepted(client):
    r = client.post(
        f"{WEBHOOK}?secret={SECRET}&symbol=MES1!",
        json={
            "action": "buy",
            "qty": 1,
            "entry": 5000.25,
            "sl": 4995.0,
            "tp1": 5005.0,
            "tp2": 5010.0,
            "order_id": "xiznit_entry_qparam_1",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["accepted"] is True
    assert body["decision"] == "accepted"
    # Off-state webhook: journaled but no broker submission.
    assert body["execution"]["broker"] == "topstep"
    assert body["execution"]["execution_mode"] == "off"
    assert body["execution"]["contracts"] == 1


def test_xiznit_entry_with_header_secret_accepted(client):
    r = client.post(
        f"{WEBHOOK}?symbol=MES1!",
        headers={"X-SignalBridge-Secret": SECRET},
        json={
            "action": "buy",
            "qty": 1,
            "entry": 5001.0,
            "order_id": "xiznit_entry_hdr_1",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["accepted"] is True
    assert body["execution"]["fill_price"] == 5001.0


def test_xiznit_entry_qty_maps_to_contracts(client):
    r = client.post(
        f"{WEBHOOK}?secret={SECRET}&symbol=MES1!",
        json={
            "action": "buy",
            "qty": 1,
            "entry": 5002.0,
            "order_id": "xiznit_qty_map_1",
        },
    )
    assert r.json()["accepted"] is True
    row = _last_signal_row(client)
    assert row["action"] == "BUY"
    assert row["contracts"] == 1


def test_xiznit_entry_symbol_in_body_overrides_query(client):
    """When the Xiznit body itself carries ``symbol``/``ticker``, the
    query-string ``symbol`` value is only a fallback."""
    r = client.post(
        f"{WEBHOOK}?secret={SECRET}&symbol=UNKNOWN_FALLBACK",
        json={
            "action": "buy",
            "symbol": "MES1!",
            "qty": 1,
            "entry": 5003.0,
            "order_id": "xiznit_symbol_body_1",
        },
    )
    body = r.json()
    assert body["accepted"] is True
    assert body["execution"]["symbol"] == "MES1!"


def test_xiznit_entry_ticker_field_accepted(client):
    """Xiznit alerts sometimes use ``ticker`` instead of ``symbol``."""
    r = client.post(
        f"{WEBHOOK}?secret={SECRET}",
        json={
            "action": "buy",
            "ticker": "MES1!",
            "qty": 1,
            "entry": 5004.0,
            "order_id": "xiznit_ticker_field_1",
        },
    )
    body = r.json()
    assert body["accepted"] is True
    assert body["execution"]["symbol"] == "MES1!"


def test_xiznit_entry_without_price_journals_dry_run(client):
    r = client.post(
        f"{WEBHOOK}?secret={SECRET}&symbol=MES1!",
        json={
            "action": "buy",
            "qty": 1,
            "order_id": "xiznit_entry_no_price_1",
        },
    )
    body = r.json()
    assert body["accepted"] is True
    assert body["execution"]["message"] == "xiznit_entry_dry_run_no_price"
    # Paper broker must NOT have generated a fill.
    positions = client.get("/api/positions").json()["open_positions"]
    assert positions == []


# ---------- TP exits ---------------------------------------------------


def test_xiznit_tp_exit_with_qty_accepted(client):
    # Open a position first so the EXIT has something to flatten.
    client.post(
        f"{WEBHOOK}?secret={SECRET}&symbol=MES1!",
        json={
            "action": "buy",
            "qty": 1,
            "entry": 5000.0,
            "order_id": "xiznit_tp_setup_1",
        },
    )
    r = client.post(
        f"{WEBHOOK}?secret={SECRET}&symbol=MES1!",
        json={
            "action": "exit",
            "tp": "TP1",
            "qty": 1,
            "price": 5005.0,
            "order_id": "xiznit_tp_exit_1",
        },
    )
    body = r.json()
    assert body["accepted"] is True
    assert body["execution"]["action"] == "EXIT"


def test_xiznit_tp_exit_without_qty_rejected(client):
    r = client.post(
        f"{WEBHOOK}?secret={SECRET}&symbol=MES1!",
        json={
            "action": "exit",
            "tp": "TP1",
            "price": 5005.0,
            "order_id": "xiznit_tp_exit_noqty_1",
        },
    )
    body = r.json()
    assert body["accepted"] is False
    assert body["rejection_reason"] == "missing_exit_qty"


# ---------- SL / force closes ------------------------------------------


def test_xiznit_sl_exit_with_qty_accepted(client):
    client.post(
        f"{WEBHOOK}?secret={SECRET}&symbol=MES1!",
        json={
            "action": "buy",
            "qty": 1,
            "entry": 5000.0,
            "order_id": "xiznit_sl_setup_1",
        },
    )
    r = client.post(
        f"{WEBHOOK}?secret={SECRET}&symbol=MES1!",
        json={
            "action": "exit",
            "reason": "sl",
            "qty": 1,
            "price": 4995.0,
            "order_id": "xiznit_sl_exit_1",
        },
    )
    body = r.json()
    assert body["accepted"] is True
    assert body["execution"]["action"] == "EXIT"


def test_xiznit_force_close_without_qty_dry_runs_close_all(client):
    r = client.post(
        f"{WEBHOOK}?secret={SECRET}&symbol=MES1!",
        json={
            "action": "exit",
            "reason": "eod_flatten",
            "order_id": "xiznit_force_close_1",
        },
    )
    body = r.json()
    assert body["accepted"] is True
    assert body["execution"]["message"] == "xiznit_exit_dry_run_close_all"
    details = body["execution"]["details"]
    assert details["xiznit"]["close_all"] is True
    assert details["xiznit"]["reason"] == "eod_flatten"


# ---------- update_sl --------------------------------------------------


def test_xiznit_update_sl_accepted_as_informational(client):
    r = client.post(
        f"{WEBHOOK}?secret={SECRET}&symbol=MES1!",
        json={
            "action": "update_sl",
            "sl": 5002.5,
            "order_id": "xiznit_sl_move_1",
        },
    )
    body = r.json()
    assert body["accepted"] is True
    assert body["execution"]["message"] == "stop_update_received"
    assert body["execution"]["details"]["stop_level"] == 5002.5
    # No paper fill should have happened.
    assert client.get("/api/positions").json()["open_positions"] == []


def test_xiznit_update_sl_without_contracts_or_price_accepted(client):
    """update_sl must not be rejected for missing contracts/price."""
    r = client.post(
        f"{WEBHOOK}?secret={SECRET}&symbol=MES1!",
        json={"action": "update_sl"},
    )
    body = r.json()
    assert body["accepted"] is True
    assert body["execution"]["message"] == "stop_update_received"


# ---------- Secret handling --------------------------------------------


def test_xiznit_missing_secret_rejected(client):
    """A request with no body secret AND no query/header secret is
    rejected as invalid_secret. The bugfix pass unified the
    missing-secret / wrong-secret paths into a single
    ``invalid_secret`` rejection since both have the same operator
    meaning (auth failed) and the centralised secret check at the
    top of handle() doesn't differentiate."""
    r = client.post(
        f"{WEBHOOK}?symbol=MES1!",
        json={"action": "buy", "qty": 1, "entry": 5000.0, "order_id": "ns1"},
    )
    body = r.json()
    assert body["accepted"] is False
    assert body["rejection_reason"] == "invalid_secret"


def test_xiznit_invalid_secret_rejected(client):
    r = client.post(
        f"{WEBHOOK}?secret=wrong-value&symbol=MES1!",
        json={"action": "buy", "qty": 1, "entry": 5000.0, "order_id": "is1"},
    )
    body = r.json()
    assert body["accepted"] is False
    assert body["rejection_reason"] == "invalid_secret"


def test_xiznit_invalid_header_secret_rejected(client):
    r = client.post(
        f"{WEBHOOK}?symbol=MES1!",
        headers={"X-SignalBridge-Secret": "wrong-value"},
        json={"action": "buy", "qty": 1, "entry": 5000.0, "order_id": "is2"},
    )
    body = r.json()
    assert body["accepted"] is False
    assert body["rejection_reason"] == "invalid_secret"


# ---------- Symbol fallback --------------------------------------------


def test_xiznit_missing_symbol_rejected(client):
    r = client.post(
        f"{WEBHOOK}?secret={SECRET}",
        json={"action": "buy", "qty": 1, "entry": 5000.0, "order_id": "ms1"},
    )
    body = r.json()
    assert body["accepted"] is False
    assert body["rejection_reason"] == "missing_symbol"


def test_xiznit_missing_symbol_uses_query_fallback(client):
    r = client.post(
        f"{WEBHOOK}?secret={SECRET}&symbol=MES1!",
        json={"action": "buy", "qty": 1, "entry": 5000.0, "order_id": "ms_fb_1"},
    )
    body = r.json()
    assert body["accepted"] is True
    assert body["execution"]["symbol"] == "MES1!"
