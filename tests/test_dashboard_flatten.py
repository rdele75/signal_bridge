"""Tests for the Dashboard flatten button + soft-confirm modal +
the structured /api/broker/flatten-all envelope on Topstep.

These focus on the things the previous-build tests can't cover:
the new button label, the absence of the legacy 'paper only' label
and TopstepX banner, the soft-confirm modal HTML, and that the
endpoint returns the new legs[] envelope shape on Topstep.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from .conftest import _build_app


def _build_topstep_app(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    selected_account: str = "5001",
):
    monkeypatch.setenv("TOPSTEP_USERNAME", "trader42")
    monkeypatch.setenv("TOPSTEP_API_KEY", "abcd1234efgh5678")
    monkeypatch.setenv("TOPSTEP_ACCOUNT_ID", selected_account)
    monkeypatch.setenv("SELECTED_ACCOUNT_ID", selected_account)
    sm_path = tmp_path / "missing_symbols.json"
    sm_path.write_text(json.dumps({"MES1!": {"topstep": "CON.F.US.MES.M26"}}))
    monkeypatch.setenv("SYMBOLS_MAP_PATH", str(sm_path))
    return _build_app(tmp_path, monkeypatch, provider="topstep")


# ----------------------------------------------------------------------
# Button label + enabled state
# ----------------------------------------------------------------------


def test_flatten_button_renders_canonical_label_on_topstep(tmp_path, monkeypatch):
    """The Flatten button uses the new canonical label and the legacy
    'Flatten (paper only)' / 'Exit All / Flatten' labels are gone.
    Same label on paper and topstep."""
    app = _build_topstep_app(tmp_path, monkeypatch)
    with TestClient(app) as c:
        body = c.get("/").text
    assert "Flatten All Positions" in body
    assert "Flatten (paper only)" not in body
    assert "Exit All / Flatten" not in body


def test_flatten_button_renders_canonical_label_on_paper(client):
    body = client.get("/").text
    assert "Flatten All Positions" in body
    assert "Flatten (paper only)" not in body
    assert "Exit All / Flatten" not in body


def test_flatten_button_is_enabled_on_topstep(tmp_path, monkeypatch):
    """No more disabled attribute or TopstepX-pointing tooltip on the
    button — flatten is real now."""
    app = _build_topstep_app(tmp_path, monkeypatch)
    with TestClient(app) as c:
        body = c.get("/").text
    start = body.find('id="btn-flatten-all"')
    assert start != -1
    tag = body[start:body.find(">", start)]
    assert "disabled" not in tag, tag
    assert "not yet implemented" not in tag
    assert "TopstepX" not in tag


def test_legacy_topstep_flatten_banner_is_gone(tmp_path, monkeypatch):
    """The 'No emergency flatten available for Topstep yet' banner that
    pointed operators at the TopstepX app must be removed."""
    app = _build_topstep_app(tmp_path, monkeypatch)
    with TestClient(app) as c:
        body = c.get("/").text
    assert "execution-flatten-topstep-note" not in body
    assert "No emergency flatten available" not in body


# ----------------------------------------------------------------------
# Soft-confirm modal HTML
# ----------------------------------------------------------------------


def test_flatten_soft_confirm_modal_markup_is_present(client):
    """The soft-confirm modal must ship with the dashboard so the JS
    can open it on click. Required structure:
      - the modal container by id
      - the red primary action with the 'Yes, flatten now' label
      - a Cancel button
      - a positions container the JS fills with the fetched list"""
    body = client.get("/").text
    assert 'id="flatten-modal"' in body
    assert 'id="btn-flatten-confirm"' in body
    assert "Yes, flatten now" in body
    assert 'id="flatten-modal-positions"' in body
    # Cancel button uses the shared data-close-modal hook so the
    # existing modal dismissal handlers pick it up.
    modal_start = body.find('id="flatten-modal"')
    modal_end = body.find("</div>", modal_start + 1000)
    modal_block = body[modal_start:modal_end + 100]
    assert "data-close-modal" in modal_block
    assert ">Cancel<" in modal_block


def test_flatten_soft_confirm_modal_does_not_require_phrase_typing(client):
    """Unlike the live-engage / smoke-execute modals, the flatten
    soft-confirm modal does NOT require a typed phrase — operator
    confidence comes from seeing the positions, not typing a word."""
    body = client.get("/").text
    # No phrase input inside the flatten modal block.
    start = body.find('id="flatten-modal"')
    end = body.find('id="flatten-modal-title"')
    # Pull the whole modal markup by scanning past the title block.
    end_block = body.find("</div>\n</div>", start)
    modal_block = body[start:end_block + 12]
    assert 'type="text"' not in modal_block
    assert 'name="confirm"' not in modal_block


def test_flatten_results_panel_markup_is_present(client):
    """The per-leg results panel must ship in the rendered HTML so the
    JS can populate it after a confirmed flatten."""
    body = client.get("/").text
    assert 'id="flatten-results-panel"' in body
    assert 'id="flatten-results-legs"' in body
    assert 'id="flatten-results-summary"' in body
    assert 'id="flatten-results-close"' in body


# ----------------------------------------------------------------------
# /api/broker/flatten-all envelope shape on Topstep
# ----------------------------------------------------------------------


def test_flatten_all_topstep_demo_returns_legs_envelope(tmp_path, monkeypatch):
    """In demo mode (the default Topstep fixture state), flatten-all
    must return the new structured envelope — ok=False, status=
    not_in_live_mode, an empty legs list — not the legacy
    not_implemented shape."""
    app = _build_topstep_app(tmp_path, monkeypatch)
    with TestClient(app) as c:
        r = c.post("/api/broker/flatten-all")
    assert r.status_code == 200
    body = r.json()
    assert body["provider"] == "topstep"
    assert body["status"] == "not_in_live_mode"
    assert body["ok"] is False
    assert "legs" in body
    assert body["legs"] == []
    # Legacy keys must NOT be set — there's no longer a stub envelope.
    assert body.get("not_implemented") is not True


def test_flatten_all_topstep_live_returns_per_leg_envelope(
    tmp_path, monkeypatch
):
    """When the Topstep adapter is armed for live + the underlying
    /api/Position/searchOpen + /api/Position/closeContract POSTs are
    mocked to succeed, the endpoint returns the per-leg envelope: a
    leg per position, top-level status=flattened, ok=True."""
    app = _build_topstep_app(tmp_path, monkeypatch)
    # _build_app reloads app.* — reach for the fresh TopstepBroker.
    from app.execution.topstep import TopstepBroker as _Topstep

    def _fake_post(self, path, payload, *, auth=False):
        if path == "/api/Auth/loginKey":
            return 200, {"success": True, "token": "JWT.MOCK",
                         "errorCode": 0, "errorMessage": None}
        if path == "/api/Position/searchOpen":
            return 200, {
                "success": True, "errorCode": 0, "errorMessage": None,
                "positions": [
                    {"id": 1, "accountId": 5001,
                     "contractId": "CON.F.US.MES.M26",
                     "type": 1, "size": 1},
                    {"id": 2, "accountId": 5001,
                     "contractId": "CON.F.US.MNQ.M26",
                     "type": 2, "size": 1},
                ],
            }
        if path == "/api/Position/closeContract":
            return 200, {
                "success": True, "errorCode": 0,
                "errorMessage": None, "orderId": 4242,
            }
        return 200, {"success": False, "errorCode": -1,
                     "errorMessage": "unhandled"}

    monkeypatch.setattr(_Topstep, "_post_json", _fake_post)

    # Arm the broker for live execution from the in-process settings.
    s = app.state.settings
    s.enable_topstep_order_execution = True
    s.topstep_execution_confirm = "LIVE_CONFIRMED"
    s.enable_live_trading = True
    s.live_trading_confirm = "I_UNDERSTAND_LIVE_ORDERS"
    s.live_trading_account_ack = True
    s.execution_mode = "live"
    # Mirror onto the broker so the gates see the same state.
    b = app.state.broker
    b.enable_order_execution = True
    b.execution_confirm = "LIVE_CONFIRMED"
    b.enable_live_trading = True
    b.live_trading_confirm = "I_UNDERSTAND_LIVE_ORDERS"
    b.live_trading_account_ack = True
    b.execution_mode = "live"
    b._can_trade_cache[str(b.account_id)] = True

    with TestClient(app) as c:
        r = c.post("/api/broker/flatten-all")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["status"] == "flattened"
    assert body["provider"] == "topstep"
    assert isinstance(body["legs"], list)
    assert len(body["legs"]) == 2
    assert all(leg["ok"] for leg in body["legs"])
    # First leg was a long → closing side is SELL.
    assert body["legs"][0]["side"] == "SELL"
    assert body["legs"][0]["order_id"] == "4242"


def test_flatten_all_endpoint_requires_admin_when_auth_on(
    tmp_path, monkeypatch
):
    """Regression: even with the new real flatten implementation, the
    admin-auth gate still applies."""
    monkeypatch.setenv("TOPSTEP_USERNAME", "trader42")
    monkeypatch.setenv("TOPSTEP_API_KEY", "abcd1234efgh5678")
    monkeypatch.setenv("TOPSTEP_ACCOUNT_ID", "5001")
    app = _build_app(
        tmp_path, monkeypatch, provider="topstep",
        admin_auth_enabled=True,
    )
    with TestClient(app) as c:
        r = c.post("/api/broker/flatten-all")
    assert r.status_code == 401
