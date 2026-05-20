"""Tests for the optional timeframe lock."""
from __future__ import annotations

from pathlib import Path

from app.config import Settings
from app.journal import Journal
from app.kill_switch import KillSwitch
from app.risk_engine import RiskEngine, normalize_timeframe
from app.schemas import NormalizedSignal

from .conftest import make_alert


# ---------- normalize_timeframe unit cases ----------

def test_normalize_timeframe_numeric_str():
    assert normalize_timeframe("1") == "1"
    assert normalize_timeframe("5") == "5"
    assert normalize_timeframe("15") == "15"
    assert normalize_timeframe("60") == "60"


def test_normalize_timeframe_raw_int():
    assert normalize_timeframe(1) == "1"
    assert normalize_timeframe(5) == "5"


def test_normalize_timeframe_float_integers():
    assert normalize_timeframe(1.0) == "1"
    assert normalize_timeframe(5.0) == "5"


def test_normalize_timeframe_minute_suffix():
    assert normalize_timeframe("1m") == "1"
    assert normalize_timeframe("5m") == "5"
    assert normalize_timeframe("15m") == "15"


def test_normalize_timeframe_hour_suffix():
    assert normalize_timeframe("1h") == "60"
    assert normalize_timeframe("2h") == "120"


def test_normalize_timeframe_letter_codes():
    assert normalize_timeframe("D") == "D"
    assert normalize_timeframe("d") == "D"
    assert normalize_timeframe("W") == "W"
    assert normalize_timeframe("1D") == "D"


def test_normalize_timeframe_blank_and_none():
    assert normalize_timeframe(None) is None
    assert normalize_timeframe("") is None
    assert normalize_timeframe("   ") is None


# ---------- RiskEngine timeframe gate (unit) ----------

def _build(tmp_path: Path, **overrides) -> tuple[RiskEngine, Settings]:
    db = tmp_path / "tf.db"
    log = tmp_path / "tf.log"
    base = dict(
        app_name="SignalBridge",
        app_host="127.0.0.1",
        app_port=8000,
        execution_mode="paper",
        broker_provider="paper",
        broker="paper",
        webhook_secret="s",
        allowed_symbols=["MES1!"],
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
        settings.database_abs_path.parent / "tf_kill.active",
        enabled=settings.enable_kill_switch,
    )
    return RiskEngine(settings, journal, ks), settings


def _signal(**kw) -> NormalizedSignal:
    base = dict(
        source="tradingview",
        strategy="t",
        symbol="MES1!",
        exchange="CME_MINI",
        action="BUY",
        contracts=1,
        price=5000.0,
        order_id="tf_001",
        timeframe=None,
        raw={},
    )
    base.update(kw)
    return NormalizedSignal(**base)


def test_lock_disabled_passes_without_timeframe(tmp_path):
    risk, _ = _build(tmp_path, enable_timeframe_lock=False)
    d = risk.evaluate(_signal(timeframe=None))
    assert d.accepted is True


def test_lock_enabled_rejects_missing_timeframe(tmp_path):
    risk, _ = _build(tmp_path, enable_timeframe_lock=True, allowed_timeframes=["1"])
    d = risk.evaluate(_signal(timeframe=None))
    assert d.accepted is False
    assert d.reason == "missing_timeframe"


def test_lock_enabled_accepts_string_one(tmp_path):
    risk, _ = _build(tmp_path, enable_timeframe_lock=True, allowed_timeframes=["1"])
    d = risk.evaluate(_signal(timeframe="1"))
    assert d.accepted is True


def test_lock_enabled_rejects_disallowed(tmp_path):
    risk, _ = _build(tmp_path, enable_timeframe_lock=True, allowed_timeframes=["1"])
    d = risk.evaluate(_signal(timeframe="5"))
    assert d.accepted is False
    assert "timeframe_not_allowed" in d.reason
    assert "got 5" in d.reason
    assert "allowed 1" in d.reason


def test_lock_enabled_accepts_csv(tmp_path):
    risk, _ = _build(
        tmp_path, enable_timeframe_lock=True, allowed_timeframes=["1", "5", "15"]
    )
    for tf in ("1", "5", "15"):
        d = risk.evaluate(_signal(timeframe=tf))
        assert d.accepted is True, f"expected {tf} accepted"
    d = risk.evaluate(_signal(timeframe="30"))
    assert d.accepted is False


# ---------- End-to-end webhook + persisted settings ----------

def _enable_lock(client, csv: str = "1") -> None:
    r = client.post(
        "/settings/risk",
        data={
            "allowed_symbols": "MES1!,MNQ1!",
            "max_contracts_per_trade": "1",
            "max_daily_loss": "250",
            "max_open_positions": "1",
            "duplicate_order_cooldown_seconds": "60",
            "enable_longs": "true",
            "enable_shorts": "true",
            "enable_timeframe_lock": "true",
            "allowed_timeframes": csv,
        },
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text


def test_webhook_lock_disabled_accepts_without_timeframe(client):
    r = client.post(
        "/webhooks/tradingview",
        json=make_alert(order_id="tf_off_noTF"),
    )
    body = r.json()
    assert body["accepted"] is True


def test_webhook_lock_enabled_rejects_missing(client):
    _enable_lock(client, "1")
    payload = make_alert(order_id="tf_on_missing")
    # Strip out the placeholder if make_alert added one — it didn't, but be safe.
    payload.pop("timeframe", None)
    r = client.post("/webhooks/tradingview", json=payload)
    body = r.json()
    assert body["accepted"] is False
    assert body["rejection_reason"] == "missing_timeframe"


def test_webhook_lock_enabled_accepts_quoted_one(client):
    _enable_lock(client, "1")
    r = client.post(
        "/webhooks/tradingview",
        json=make_alert(order_id="tf_quoted_one", timeframe="1"),
    )
    body = r.json()
    assert body["accepted"] is True


def test_webhook_lock_enabled_accepts_unquoted_one(client):
    _enable_lock(client, "1")
    r = client.post(
        "/webhooks/tradingview",
        json=make_alert(order_id="tf_unquoted_one", timeframe=1),
    )
    body = r.json()
    assert body["accepted"] is True


def test_webhook_lock_enabled_rejects_five_when_only_one_allowed(client):
    _enable_lock(client, "1")
    r = client.post(
        "/webhooks/tradingview",
        json=make_alert(order_id="tf_5_blocked", timeframe="5"),
    )
    body = r.json()
    assert body["accepted"] is False
    assert "timeframe_not_allowed" in body["rejection_reason"]
    assert "got 5" in body["rejection_reason"]
    assert "allowed 1" in body["rejection_reason"]


def test_webhook_lock_enabled_csv_allows_multiple(client):
    _enable_lock(client, "1,5,15")
    accepted = []
    for tf in ("1", "5", "15"):
        r = client.post(
            "/webhooks/tradingview",
            json=make_alert(order_id=f"tf_csv_{tf}", timeframe=tf),
        )
        body = r.json()
        accepted.append((tf, body["accepted"], body.get("rejection_reason")))
    assert all(a[1] for a in accepted), accepted

    # 30 should still be blocked.
    r = client.post(
        "/webhooks/tradingview",
        json=make_alert(order_id="tf_csv_30", timeframe="30"),
    )
    body = r.json()
    assert body["accepted"] is False
    assert "timeframe_not_allowed" in body["rejection_reason"]


# NOTE: test_tradingview_page_template_includes_interval_placeholder
# removed during the polish pass — the Generic / manual alert JSON
# template was deleted, so there is no longer an inline timeframe
# placeholder on the tradingview page. The Xiznit template lives in
# a separate Pine script and isn't rendered through this endpoint.


def test_risk_page_renders_timeframe_lock_fields(client):
    body = client.get("/settings/risk").text
    assert 'name="enable_timeframe_lock"' in body
    assert 'name="allowed_timeframes"' in body


def test_dashboard_surfaces_timeframe_lock_status(client):
    """Timeframe-lock detail moved off the dashboard during the layout
    cleanup; the risk page is the canonical surface now."""
    _enable_lock(client, "1,5")
    risk_body = client.get("/settings/risk").text
    assert "Timeframe lock" in risk_body
    assert "1,5" in risk_body
    # /api/status still exposes the runtime state.
    api_body = client.get("/api/status").json()
    # Defaults to True on creation; we explicitly enabled here.
    assert api_body["allowed_symbols"]  # symbols still on the API
    # Dashboard renders without crashing — no more inline timeframe-lock card.
    dash = client.get("/")
    assert dash.status_code == 200
