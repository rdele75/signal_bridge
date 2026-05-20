"""Tests for the Symbol Settings page + Topstep contract search endpoint.

Covers:
  * /settings/symbols page loads and lists default mappings
  * sidebar includes the Symbols link
  * config/symbols.example.json carries MNQ/MES/NQ/ES defaults
  * SymbolMap loader handles the bundled mappings
  * POST /settings/symbols persists to config/symbols.json
  * blank topstep entries for NQ1!/ES1! don't crash the loader
  * order builder rejects blank topstep mapping with symbol_mapping_missing
  * /api/topstep/contracts/search requires admin auth
  * /api/topstep/contracts/search forwards searchText/live to ProjectX
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.execution.topstep import TopstepBroker
from app.execution.topstep_order_builder import build_market_order_payload
from app.schemas import NormalizedSignal
from app.symbol_map import SymbolMap, parse_form_mappings

from .conftest import ADMIN_PASSWORD, login_as_admin


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _signal(symbol: str = "ES1!", **overrides) -> NormalizedSignal:
    base = dict(
        source="tradingview",
        strategy="t",
        symbol=symbol,
        broker_symbol=symbol,
        exchange=None,
        action="BUY",
        contracts=1,
        price=None,
        order_id=None,
        comment=None,
        timeframe=None,
        raw={},
    )
    base.update(overrides)
    return NormalizedSignal(**base)


# ---------------------------------------------------------------------------
# Config files: bundled defaults
# ---------------------------------------------------------------------------


def test_symbols_example_json_includes_mnq_mes_nq_es():
    data = json.loads((PROJECT_ROOT / "config" / "symbols.example.json").read_text())
    for ticker in ("MNQ1!", "MES1!", "NQ1!", "ES1!"):
        assert ticker in data, f"{ticker} missing from symbols.example.json"
    # ProjectX contract ids for MNQ/MES are populated; NQ/ES are
    # placeholders that the operator must fill via the dashboard.
    assert data["MNQ1!"]["topstep"] == "CON.F.US.MNQ.M26"
    assert data["MES1!"]["topstep"] == "CON.F.US.MES.M26"
    assert data["NQ1!"]["topstep"] == ""
    assert data["ES1!"]["topstep"] == ""


def test_symbols_json_includes_mnq_mes_nq_es():
    data = json.loads((PROJECT_ROOT / "config" / "symbols.json").read_text())
    for ticker in ("MNQ1!", "MES1!", "NQ1!", "ES1!"):
        assert ticker in data


# ---------------------------------------------------------------------------
# SymbolMap loader: resolves what's set, returns None when blank
# ---------------------------------------------------------------------------


def test_symbol_map_loads_default_topstep_mappings(tmp_path: Path):
    p = tmp_path / "symbols.json"
    p.write_text(json.dumps({
        "MNQ1!": {"paper": "MNQ1!", "topstep": "CON.F.US.MNQ.M26"},
        "MES1!": {"paper": "MES1!", "topstep": "CON.F.US.MES.M26"},
        "NQ1!":  {"paper": "NQ1!",  "topstep": ""},
        "ES1!":  {"paper": "ES1!",  "topstep": ""},
    }))
    sm = SymbolMap(p)
    assert sm.resolve_explicit("MNQ1!", "topstep") == "CON.F.US.MNQ.M26"
    assert sm.resolve_explicit("MES1!", "topstep") == "CON.F.US.MES.M26"
    # Blank topstep entries must surface as no mapping rather than echoing
    # the raw ticker as a fake contract id.
    assert sm.resolve_explicit("NQ1!", "topstep") is None
    assert sm.resolve_explicit("ES1!", "topstep") is None


def test_symbol_map_legacy_tradovate_keys_are_ignored_on_load(tmp_path: Path):
    """An operator's existing config/symbols.json may still carry the
    ``tradovate`` column from a prior install. The loader must not error
    on those keys and must not surface them through ``all_mappings()``.
    """
    p = tmp_path / "symbols.json"
    p.write_text(json.dumps({
        "MES1!": {"paper": "MES1!", "topstep": "CON.F.US.MES.M26", "tradovate": "MES"},
    }))
    sm = SymbolMap(p)
    rows = sm.all_mappings()
    assert rows == {"MES1!": {"paper": "MES1!", "topstep": "CON.F.US.MES.M26"}}
    assert "tradovate" not in rows["MES1!"]


def test_symbol_map_all_mappings_filters_metadata_keys(tmp_path: Path):
    p = tmp_path / "symbols.json"
    p.write_text(json.dumps({
        "_comment": "ignored by the UI",
        "_warning": "also ignored",
        "ES1!": {"paper": "ES1!", "topstep": ""},
    }))
    sm = SymbolMap(p)
    rows = sm.all_mappings()
    assert "_comment" not in rows
    assert "_warning" not in rows
    assert rows == {"ES1!": {"paper": "ES1!", "topstep": ""}}


def test_symbol_map_replace_all_writes_disk_and_preserves_metadata(tmp_path: Path):
    p = tmp_path / "symbols.json"
    p.write_text(json.dumps({
        "_comment": "keep me",
        "MES1!": {"paper": "MES1!", "topstep": "CON.F.US.MES.M26"},
    }))
    sm = SymbolMap(p)
    sm.replace_all({
        "MES1!": {"paper": "MES1!", "topstep": "CON.F.US.MES.M27"},
        "ES1!":  {"paper": "ES1!",  "topstep": ""},
    })
    reloaded = json.loads(p.read_text())
    assert reloaded["_comment"] == "keep me"
    assert reloaded["MES1!"]["topstep"] == "CON.F.US.MES.M27"
    assert reloaded["ES1!"]["topstep"] == ""


# ---------------------------------------------------------------------------
# parse_form_mappings: paper defaults to ticker, ticker required
# ---------------------------------------------------------------------------


def test_parse_form_mappings_paper_defaults_to_ticker():
    rows = parse_form_mappings(
        ["ES1!", ""], ["", "ignored"], ["", ""]
    )
    assert rows == {"ES1!": {"paper": "ES1!", "topstep": ""}}


def test_parse_form_mappings_mismatched_arrays_raises():
    try:
        parse_form_mappings(["ES1!"], ["ES1!"], [])
    except ValueError:
        return
    raise AssertionError("parse_form_mappings should reject mis-aligned arrays")


# ---------------------------------------------------------------------------
# Order builder: blank Topstep contract → symbol_mapping_missing with the
# new "Configuration > Symbols" message.
# ---------------------------------------------------------------------------


class _StubMap:
    def __init__(self, m): self._m = m
    def resolve_explicit(self, t, p): return self._m.get(t, {}).get(p) or None


def test_builder_blank_topstep_for_es_rejects_with_mapping_missing():
    result = build_market_order_payload(
        _signal(symbol="ES1!"),
        account_id=5001,
        symbol_map=_StubMap({
            "ES1!": {"paper": "ES1!", "topstep": ""},
        }),
    )
    assert result["ok"] is False
    assert result["reason"] == "symbol_mapping_missing"
    assert "ES1!" in result["message"]
    assert "Configuration > Symbols" in result["message"]


def test_builder_blank_topstep_for_nq_rejects_with_mapping_missing():
    result = build_market_order_payload(
        _signal(symbol="NQ1!"),
        account_id=5001,
        symbol_map=_StubMap({
            "NQ1!": {"paper": "NQ1!", "topstep": ""},
        }),
    )
    assert result["ok"] is False
    assert result["reason"] == "symbol_mapping_missing"
    assert "NQ1!" in result["message"]


def test_builder_existing_mnq_mapping_still_builds():
    result = build_market_order_payload(
        _signal(symbol="MNQ1!"),
        account_id=5001,
        symbol_map=_StubMap({
            "MNQ1!": {"paper": "MNQ1!", "topstep": "CON.F.US.MNQ.M26"},
        }),
    )
    assert result["ok"] is True
    assert result["payload"]["contractId"] == "CON.F.US.MNQ.M26"


def test_builder_existing_mes_mapping_still_builds():
    result = build_market_order_payload(
        _signal(symbol="MES1!"),
        account_id=5001,
        symbol_map=_StubMap({
            "MES1!": {"paper": "MES1!", "topstep": "CON.F.US.MES.M26"},
        }),
    )
    assert result["ok"] is True
    assert result["payload"]["contractId"] == "CON.F.US.MES.M26"


# ---------------------------------------------------------------------------
# /settings/symbols HTML routes
# ---------------------------------------------------------------------------


def _seed_default_symbols(symbols_path: Path) -> None:
    symbols_path.parent.mkdir(parents=True, exist_ok=True)
    symbols_path.write_text(json.dumps({
        "MNQ1!": {"paper": "MNQ1!", "topstep": "CON.F.US.MNQ.M26"},
        "MES1!": {"paper": "MES1!", "topstep": "CON.F.US.MES.M26"},
        "NQ1!":  {"paper": "NQ1!",  "topstep": ""},
        "ES1!":  {"paper": "ES1!",  "topstep": ""},
    }))


def test_settings_symbols_page_returns_200_and_lists_defaults(make_app, tmp_path):
    _seed_default_symbols(tmp_path / "missing_symbols.json")
    app = make_app(provider="paper")
    with TestClient(app) as c:
        r = c.get("/settings/symbols")
    assert r.status_code == 200
    body = r.text
    assert "Symbol Settings" in body
    for ticker in ("MNQ1!", "MES1!", "NQ1!", "ES1!"):
        assert ticker in body
    assert "CON.F.US.MNQ.M26" in body
    assert "CON.F.US.MES.M26" in body


def test_sidebar_includes_symbols_link(make_app, tmp_path):
    _seed_default_symbols(tmp_path / "missing_symbols.json")
    app = make_app(provider="paper")
    with TestClient(app) as c:
        body = c.get("/settings/symbols").text
    assert 'href="/settings/symbols"' in body
    # Sidebar group must auto-open since we're on a Configuration child page.
    assert '<details class="nav-group" open' in body


def test_settings_symbols_post_writes_disk(make_app, tmp_path):
    sym_path = tmp_path / "missing_symbols.json"
    _seed_default_symbols(sym_path)
    app = make_app(provider="paper")
    # HTML <input name="ticker"> arrays arrive as duplicate-key form data.
    # httpx encodes dict-of-lists into repeated keys preserving order.
    form = {
        "ticker":    ["MNQ1!",            "ES1!"],
        "paper":     ["MNQ1!",            ""],
        "topstep":   ["CON.F.US.MNQ.M26", "CON.F.US.ES.M26"],
    }
    with TestClient(app) as c:
        r = c.post("/settings/symbols", data=form, follow_redirects=False)
    assert r.status_code == 303
    on_disk = json.loads(sym_path.read_text())
    assert on_disk["MNQ1!"]["topstep"] == "CON.F.US.MNQ.M26"
    # Paper defaulted to ticker because we sent blank.
    assert on_disk["ES1!"]["paper"] == "ES1!"
    assert on_disk["ES1!"]["topstep"] == "CON.F.US.ES.M26"


def test_settings_symbols_post_updates_live_loader(make_app, tmp_path):
    sym_path = tmp_path / "missing_symbols.json"
    _seed_default_symbols(sym_path)
    app = make_app(provider="paper")
    form = {
        "ticker":    ["ES1!"],
        "paper":     ["ES1!"],
        "topstep":   ["CON.F.US.ES.M26"],
    }
    with TestClient(app) as c:
        r = c.post("/settings/symbols", data=form, follow_redirects=False)
        assert r.status_code == 303
    # The in-memory SymbolMap on app.state must reflect the new value.
    assert app.state.symbol_map.resolve_explicit("ES1!", "topstep") == "CON.F.US.ES.M26"


# ---------------------------------------------------------------------------
# /api/topstep/contracts/search
# ---------------------------------------------------------------------------


def test_contract_search_requires_admin_auth(make_app, tmp_path):
    _seed_default_symbols(tmp_path / "missing_symbols.json")
    app = make_app(provider="paper", admin_auth_enabled=True)
    with TestClient(app) as c:
        r = c.post(
            "/api/topstep/contracts/search",
            json={"searchText": "NQ", "live": False},
        )
    assert r.status_code == 401


def test_contract_search_calls_projectx_with_search_text_and_live_false(
    make_app, tmp_path, monkeypatch
):
    _seed_default_symbols(tmp_path / "missing_symbols.json")
    app = make_app(provider="paper")
    # Configure Topstep creds + a cached valid token so the endpoint
    # skips authenticate().
    settings = app.state.settings
    store = app.state.settings_store
    for key, value in [
        ("TOPSTEP_USERNAME", "trader42"),
        ("TOPSTEP_API_KEY", "abcd1234efgh5678"),
        ("TOPSTEP_TOKEN", "JWT.PRE.CACHED"),
        ("TOPSTEP_TOKEN_EXPIRES_AT", "2099-01-01T00:00:00+00:00"),
    ]:
        store.apply_to_settings(settings, key, store.update_typed(key, value))

    captured: dict = {}

    def fake_post(self, path, payload, *, auth=False):
        captured["path"] = path
        captured["payload"] = payload
        captured["auth"] = auth
        return 200, {
            "success": True,
            "errorCode": 0,
            "errorMessage": None,
            "contracts": [
                {
                    "id": "CON.F.US.ENQ.M26",
                    "name": "ENQM26",
                    "description": "E-mini Nasdaq-100: June 2026",
                    "tickSize": 0.25,
                    "tickValue": 5,
                    "activeContract": True,
                    "symbolId": "F.US.ENQ",
                },
            ],
        }

    # `make_app` reloads `app.*` modules — patch the freshly-imported
    # TopstepBroker class so the running endpoint sees the fake.
    from app.execution.topstep import TopstepBroker as FreshTopstepBroker
    monkeypatch.setattr(FreshTopstepBroker, "_post_json", fake_post)

    with TestClient(app) as c:
        r = c.post(
            "/api/topstep/contracts/search",
            json={"searchText": "NQ", "live": False},
        )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert captured["path"] == "/api/Contract/search"
    assert captured["payload"] == {"searchText": "NQ", "live": False}
    assert captured["auth"] is True
    assert len(body["contracts"]) == 1
    assert body["contracts"][0]["id"] == "CON.F.US.ENQ.M26"
    assert body["contracts"][0]["activeContract"] is True


def test_contract_search_missing_search_text_returns_envelope(make_app, tmp_path):
    _seed_default_symbols(tmp_path / "missing_symbols.json")
    app = make_app(provider="paper")
    with TestClient(app) as c:
        r = c.post(
            "/api/topstep/contracts/search",
            json={"searchText": "", "live": False},
        )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["status"] == "missing_search_text"
    assert body["contracts"] == []


def test_contract_search_missing_credentials_envelope(make_app, tmp_path):
    _seed_default_symbols(tmp_path / "missing_symbols.json")
    app = make_app(provider="paper")
    # Default fixture has blank Topstep creds — verify graceful refusal.
    with TestClient(app) as c:
        r = c.post(
            "/api/topstep/contracts/search",
            json={"searchText": "NQ", "live": False},
        )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["status"] == "missing_credentials"
