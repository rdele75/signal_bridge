"""Tests for the Topstep adapter's real flatten / cancel implementations.

Every test monkey-patches ``TopstepBroker._post_json`` so the suite
never hits topstepx.com. The flatten / cancel paths are the most
safety-critical write APIs in this build — these tests cover the
expected envelope shape, partial-failure behavior, the kill-switch
bypass, the demo-mode no-op, the symbol filter, and the one-shot
auth retry.
"""
from __future__ import annotations

from typing import Any

from app.execution.topstep import TopstepBroker


# ----------------------------------------------------------------------
# Fixtures: a broker armed for live execution
# ----------------------------------------------------------------------


def _ready_live_broker() -> TopstepBroker:
    """A TopstepBroker with every live safety gate satisfied except
    the kill switch (off by default), credentials in place, and a
    cached valid token so the safety gate is the only thing standing
    between a flatten call and a closeContract POST."""
    b = TopstepBroker(
        username="trader42",
        api_key="abcd1234efgh5678",
        account_id="5001",
        token="JWT.PRE.CACHED",
        token_expires_at="2099-01-01T00:00:00+00:00",
        execution_mode="live",
        enable_order_execution=True,
        enable_live_trading=True,
        execution_confirm="LIVE_CONFIRMED",
        live_trading_confirm="I_UNDERSTAND_LIVE_ORDERS",
        live_trading_account_ack=True,
    )
    # canTrade cache populated so the gate passes.
    b._can_trade_cache["5001"] = True
    return b


def _pos(
    contract_id: str = "CON.F.US.MES.M26",
    size: int = 2,
    pos_type: int = 1,
    account_id: int = 5001,
) -> dict[str, Any]:
    """Position row in ProjectX's shape. ``type`` 1=long, 2=short."""
    return {
        "id": hash(contract_id) & 0xFFFFFF,
        "accountId": account_id,
        "contractId": contract_id,
        "type": pos_type,
        "size": size,
        "averagePrice": 5000.0,
    }


def _positions_ok(*positions) -> dict[str, Any]:
    return {
        "success": True,
        "errorCode": 0,
        "errorMessage": None,
        "positions": list(positions),
    }


def _orders_ok(*orders) -> dict[str, Any]:
    return {
        "success": True,
        "errorCode": 0,
        "errorMessage": None,
        "orders": list(orders),
    }


# ----------------------------------------------------------------------
# flatten_position: live mode, happy path
# ----------------------------------------------------------------------


def test_flatten_position_closes_each_open_position(monkeypatch):
    """2 open positions → 2 closeContract POSTs, both succeed,
    top-level ok=True, status=flattened."""
    calls: list[tuple[str, dict[str, Any]]] = []

    def fake_post(self, path, payload, *, auth=False):
        calls.append((path, payload))
        if path == "/api/Position/searchOpen":
            return 200, _positions_ok(
                _pos("CON.F.US.MES.M26", size=2, pos_type=1),
                _pos("CON.F.US.MNQ.M26", size=1, pos_type=2),
            )
        if path == "/api/Position/closeContract":
            order_id = 7100 + len([c for c in calls if c[0] == path])
            return 200, {"success": True, "errorCode": 0,
                         "errorMessage": None, "orderId": order_id}
        raise AssertionError(f"unexpected path: {path}")

    monkeypatch.setattr(TopstepBroker, "_post_json", fake_post)
    broker = _ready_live_broker()
    result = broker.flatten_position()

    assert result["ok"] is True
    assert result["status"] == "flattened"
    assert result["provider"] == "topstep"
    assert result["positions_before"] == 2
    assert len(result["legs"]) == 2
    assert all(leg["ok"] for leg in result["legs"])
    # First leg was a long → closing side is SELL.
    assert result["legs"][0]["side"] == "SELL"
    assert result["legs"][0]["contract_id"] == "CON.F.US.MES.M26"
    assert result["legs"][0]["order_id"] is not None
    # Second leg was a short → closing side is BUY.
    assert result["legs"][1]["side"] == "BUY"
    # closeContract was called exactly twice, with the right account id.
    close_calls = [c for c in calls if c[0] == "/api/Position/closeContract"]
    assert len(close_calls) == 2
    for _, payload in close_calls:
        assert payload["accountId"] == 5001
        assert "contractId" in payload


def test_flatten_position_partial_failure_reports_per_leg(monkeypatch):
    """3 positions, the middle one is rejected by ProjectX → top-level
    ok=False, status=partial, legs preserve the order and report each
    one's outcome independently. Legs after the failure are still
    attempted."""

    def fake_post(self, path, payload, *, auth=False):
        if path == "/api/Position/searchOpen":
            return 200, _positions_ok(
                _pos("CON.F.US.MES.M26", size=1, pos_type=1),
                _pos("CON.F.US.MNQ.M26", size=2, pos_type=1),
                _pos("CON.F.US.MGC.M26", size=1, pos_type=2),
            )
        if path == "/api/Position/closeContract":
            cid = payload["contractId"]
            if cid == "CON.F.US.MNQ.M26":
                return 200, {
                    "success": False,
                    "errorCode": 9,
                    "errorMessage": "broker rejected: max DD breach",
                    "orderId": None,
                }
            return 200, {
                "success": True,
                "errorCode": 0,
                "errorMessage": None,
                "orderId": 9999,
            }
        raise AssertionError(f"unexpected path: {path}")

    monkeypatch.setattr(TopstepBroker, "_post_json", fake_post)
    broker = _ready_live_broker()
    result = broker.flatten_position()

    assert result["ok"] is False
    assert result["status"] == "partial"
    assert len(result["legs"]) == 3
    assert result["legs"][0]["ok"] is True
    assert result["legs"][1]["ok"] is False
    assert result["legs"][1]["error_code"] == 9
    assert "max DD breach" in result["legs"][1]["message"]
    # Third leg was still attempted after the second failed.
    assert result["legs"][2]["ok"] is True
    assert "flattened 2 of 3" in result["message"]


def test_flatten_position_all_legs_fail_reports_failed(monkeypatch):
    """Every leg rejected → status=failed, ok=False."""

    def fake_post(self, path, payload, *, auth=False):
        if path == "/api/Position/searchOpen":
            return 200, _positions_ok(
                _pos("CON.F.US.MES.M26", size=1, pos_type=1),
                _pos("CON.F.US.MNQ.M26", size=2, pos_type=1),
            )
        if path == "/api/Position/closeContract":
            return 200, {
                "success": False,
                "errorCode": 5,
                "errorMessage": "rejected",
                "orderId": None,
            }
        raise AssertionError(f"unexpected path: {path}")

    monkeypatch.setattr(TopstepBroker, "_post_json", fake_post)
    broker = _ready_live_broker()
    result = broker.flatten_position()

    assert result["ok"] is False
    assert result["status"] == "failed"
    assert all(not leg["ok"] for leg in result["legs"])


# ----------------------------------------------------------------------
# flatten_position: empty cases
# ----------------------------------------------------------------------


def test_flatten_position_no_open_positions(monkeypatch):
    """Empty position list → ok=True, status=no_open_positions, no
    legs and no closeContract calls."""
    calls: list[str] = []

    def fake_post(self, path, payload, *, auth=False):
        calls.append(path)
        if path == "/api/Position/searchOpen":
            return 200, _positions_ok()
        raise AssertionError(f"unexpected path: {path}")

    monkeypatch.setattr(TopstepBroker, "_post_json", fake_post)
    broker = _ready_live_broker()
    result = broker.flatten_position()

    assert result["ok"] is True
    assert result["status"] == "no_open_positions"
    assert result["legs"] == []
    assert result["positions_before"] == 0
    # closeContract was never invoked.
    assert "/api/Position/closeContract" not in calls


def test_flatten_position_symbol_filter_only_closes_matches(monkeypatch):
    """3 positions across 3 contract ids, ``symbol='MES'`` → exactly
    one leg, others untouched. closeContract called once."""
    close_calls: list[dict[str, Any]] = []

    def fake_post(self, path, payload, *, auth=False):
        if path == "/api/Position/searchOpen":
            return 200, _positions_ok(
                _pos("CON.F.US.MES.M26", size=1, pos_type=1),
                _pos("CON.F.US.MNQ.M26", size=1, pos_type=1),
                _pos("CON.F.US.MGC.M26", size=1, pos_type=2),
            )
        if path == "/api/Position/closeContract":
            close_calls.append(payload)
            return 200, {"success": True, "errorCode": 0,
                         "errorMessage": None, "orderId": 1}
        raise AssertionError(f"unexpected path: {path}")

    monkeypatch.setattr(TopstepBroker, "_post_json", fake_post)
    broker = _ready_live_broker()
    result = broker.flatten_position(symbol="MES")

    assert result["ok"] is True
    assert result["status"] == "flattened"
    assert len(result["legs"]) == 1
    assert result["legs"][0]["contract_id"] == "CON.F.US.MES.M26"
    assert len(close_calls) == 1
    assert close_calls[0]["contractId"] == "CON.F.US.MES.M26"
    assert result["positions_before"] == 3


def test_flatten_position_symbol_filter_no_matches(monkeypatch):
    """Symbol filter matches no open position → status=no_open_positions
    with a clarifying message that says how many positions were skipped.
    No closeContract calls."""
    close_calls: list[dict[str, Any]] = []

    def fake_post(self, path, payload, *, auth=False):
        if path == "/api/Position/searchOpen":
            return 200, _positions_ok(
                _pos("CON.F.US.MNQ.M26", size=1, pos_type=1),
            )
        if path == "/api/Position/closeContract":
            close_calls.append(payload)
            return 200, {"success": True, "orderId": 1}
        raise AssertionError(f"unexpected path: {path}")

    monkeypatch.setattr(TopstepBroker, "_post_json", fake_post)
    broker = _ready_live_broker()
    result = broker.flatten_position(symbol="MES1!")

    assert result["ok"] is True
    assert result["status"] == "no_open_positions"
    assert result["legs"] == []
    assert "untouched" in result["message"]
    assert close_calls == []


# ----------------------------------------------------------------------
# flatten_position: safety gates
# ----------------------------------------------------------------------


def test_flatten_position_bypasses_kill_switch(monkeypatch):
    """Kill switch active must not block flatten — exits are how the
    operator wants to USE a kill switch in practice. Flatten still
    fires."""
    close_calls: list[dict[str, Any]] = []

    def fake_post(self, path, payload, *, auth=False):
        if path == "/api/Position/searchOpen":
            return 200, _positions_ok(
                _pos("CON.F.US.MES.M26", size=1, pos_type=1),
            )
        if path == "/api/Position/closeContract":
            close_calls.append(payload)
            return 200, {"success": True, "orderId": 5151}
        raise AssertionError(f"unexpected path: {path}")

    monkeypatch.setattr(TopstepBroker, "_post_json", fake_post)
    broker = _ready_live_broker()
    broker.kill_switch_active = True  # would block submit_market_order

    result = broker.flatten_position()
    assert result["ok"] is True
    assert result["status"] == "flattened"
    assert len(close_calls) == 1


def test_flatten_position_demo_mode_does_not_submit(monkeypatch):
    """Demo / dry-run mode → status=not_in_live_mode, no HTTP calls."""
    calls: list[str] = []

    def fake_post(self, path, payload, *, auth=False):
        calls.append(path)
        return 200, _positions_ok()

    monkeypatch.setattr(TopstepBroker, "_post_json", fake_post)
    broker = _ready_live_broker()
    broker.execution_mode = "demo"

    result = broker.flatten_position()
    assert result["ok"] is False
    assert result["status"] == "not_in_live_mode"
    assert "TopstepX" in result["message"]
    assert result["legs"] == []
    # No HTTP calls of any kind in demo mode.
    assert calls == []


def test_flatten_position_refused_when_account_ack_missing(monkeypatch):
    """Account ack still gates flatten. Same labelling as
    submit_market_order — if you can't enter live, you can't flatten
    live."""
    calls: list[str] = []

    def fake_post(self, path, payload, *, auth=False):
        calls.append(path)
        return 200, _positions_ok()

    monkeypatch.setattr(TopstepBroker, "_post_json", fake_post)
    broker = _ready_live_broker()
    broker.live_trading_account_ack = False  # gate is open

    result = broker.flatten_position()
    assert result["ok"] is False
    assert result["status"] == "live_account_ack_missing"
    assert result["gate"] == "live_account_ack_missing"
    assert result["legs"] == []
    # No HTTP traffic when the gate refuses.
    assert calls == []


def test_flatten_position_refused_when_live_trading_disabled(monkeypatch):
    calls: list[str] = []

    def fake_post(self, path, payload, *, auth=False):
        calls.append(path)
        return 200, _positions_ok()

    monkeypatch.setattr(TopstepBroker, "_post_json", fake_post)
    broker = _ready_live_broker()
    broker.enable_live_trading = False

    result = broker.flatten_position()
    assert result["ok"] is False
    assert result["status"] == "live_trading_disabled"
    assert calls == []


# ----------------------------------------------------------------------
# flatten_position: auth retry mid-flatten
# ----------------------------------------------------------------------


def test_flatten_position_retries_once_on_auth_failure(monkeypatch):
    """First leg returns HTTP 401 → adapter re-authenticates and
    retries the SAME leg. Second leg proceeds without an extra auth
    call. Tests both that the retry happens and that it's bounded
    (one shot, not a loop)."""
    state = {"close_attempts_first_leg": 0}

    def fake_post(self, path, payload, *, auth=False):
        if path == "/api/Auth/loginKey":
            return 200, {
                "success": True, "token": "JWT.REFRESHED",
                "errorCode": 0, "errorMessage": None,
            }
        if path == "/api/Position/searchOpen":
            return 200, _positions_ok(
                _pos("CON.F.US.MES.M26", size=1, pos_type=1),
                _pos("CON.F.US.MNQ.M26", size=1, pos_type=2),
            )
        if path == "/api/Position/closeContract":
            if payload["contractId"] == "CON.F.US.MES.M26":
                state["close_attempts_first_leg"] += 1
                if state["close_attempts_first_leg"] == 1:
                    return 401, {"success": False, "errorCode": 1,
                                 "errorMessage": "Unauthorized"}
                return 200, {"success": True, "orderId": 8001}
            return 200, {"success": True, "orderId": 8002}
        raise AssertionError(f"unexpected path: {path}")

    monkeypatch.setattr(TopstepBroker, "_post_json", fake_post)
    broker = _ready_live_broker()
    result = broker.flatten_position()

    assert result["ok"] is True
    assert result["status"] == "flattened"
    # First leg saw two POSTs (401 then 200), retry happened.
    assert state["close_attempts_first_leg"] == 2
    assert result["legs"][0]["ok"] is True
    assert result["legs"][1]["ok"] is True


# ----------------------------------------------------------------------
# flatten_position: failure modes that don't touch the wire
# ----------------------------------------------------------------------


def test_flatten_position_aborts_when_positions_fetch_fails(monkeypatch):
    """If /api/Position/searchOpen errors, flatten must NOT proceed to
    closeContract (there's nothing to close from)."""
    close_calls: list[Any] = []

    def fake_post(self, path, payload, *, auth=False):
        if path == "/api/Position/searchOpen":
            return 200, {"success": False, "errorCode": 5,
                         "errorMessage": "broker_down", "positions": []}
        if path == "/api/Position/closeContract":
            close_calls.append(payload)
            return 200, {"success": True, "orderId": 1}
        raise AssertionError(f"unexpected path: {path}")

    monkeypatch.setattr(TopstepBroker, "_post_json", fake_post)
    broker = _ready_live_broker()
    result = broker.flatten_position()

    assert result["ok"] is False
    assert "flatten aborted" in result["message"]
    assert result["legs"] == []
    assert close_calls == []


def test_flatten_position_envelope_omits_secrets(monkeypatch):
    """The returned envelope must not echo the API key or the cached
    JWT anywhere."""

    def fake_post(self, path, payload, *, auth=False):
        if path == "/api/Position/searchOpen":
            return 200, _positions_ok(
                _pos("CON.F.US.MES.M26", size=1, pos_type=1),
            )
        if path == "/api/Position/closeContract":
            return 200, {"success": True, "orderId": 1}
        raise AssertionError(f"unexpected path: {path}")

    monkeypatch.setattr(TopstepBroker, "_post_json", fake_post)
    broker = _ready_live_broker()
    result = broker.flatten_position()

    assert "abcd1234efgh5678" not in str(result)
    assert "JWT.PRE.CACHED" not in str(result)
