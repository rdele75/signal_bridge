"""Tests for the dashboard / layout cleanup pass.

These cover the UI changes spelled out in the task brief:

  * side panel: collapsible Configuration / Activity / System groups
  * overview: kill-switch toggle in the header, no webhook card,
    trading session card, P&L (not "Paper P&L"), win-rate and total-
    points performance cards
  * broker page: "Broker Settings" heading, no paper card/controls,
    selected-account dropdown
  * risk page: fixed-contracts disabled when strategy-managed risk is on,
    no allowed-symbols field
  * tradingview page: collapsible webhook / template / curl blocks
  * metrics page: profit graph or empty-state container
  * helpers in app.dashboard: current_trading_session(),
    current_session_time(), win_rate(), total_points_percentage(),
    profit_series()
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from fastapi.testclient import TestClient

from app import dashboard as dashboard_mod
from app.journal import Journal

from .conftest import make_alert


# ---------------------------------------------------------------------------
# Routes still return 200
# ---------------------------------------------------------------------------

ROUTES_TO_CHECK = [
    "/",
    "/settings/broker",
    "/settings/risk",
    "/tradingview",
    "/metrics",
]


def test_all_cleanup_routes_return_200(client):
    for path in ROUTES_TO_CHECK:
        r = client.get(path)
        assert r.status_code == 200, f"{path} returned {r.status_code}"


# ---------------------------------------------------------------------------
# Side panel
# ---------------------------------------------------------------------------


def test_sidebar_has_collapsible_groups(client):
    body = client.get("/").text
    # Each collapsible group is rendered as a <details class="nav-group">.
    assert body.count('<details class="nav-group"') >= 3
    assert "Configuration" in body
    assert "Activity" in body
    assert "System" in body
    # Overview link stays outside the details groups.
    assert ">Overview<" in body


def test_sidebar_configuration_collapsed_by_default_on_overview(client):
    """Visiting `/` (Overview) should leave the Configuration / Activity /
    System groups collapsed (no `open` attr on those details)."""
    body = client.get("/").text
    # No 'open' attribute on any nav-group on the overview page.
    assert '<details class="nav-group" open' not in body


def test_sidebar_configuration_expanded_on_configuration_page(client):
    """Navigating to a Configuration child page auto-expands the group so
    the active link is visible."""
    body = client.get("/settings/broker").text
    assert '<details class="nav-group" open' in body


# ---------------------------------------------------------------------------
# Overview / dashboard
# ---------------------------------------------------------------------------


def test_overview_no_longer_shows_webhook_card(client):
    body = client.get("/").text
    # The old webhook card had this exact label.
    assert "Local webhook URL" not in body
    # No literal webhook URL printed on the overview page.
    assert "/webhooks/tradingview" not in body


def test_overview_shows_header_kill_switch_toggle(client):
    body = client.get("/").text
    assert 'id="header-killswitch"' in body
    # Toggle reflects state via the existing API.
    assert "kill switch off" in body


def test_header_kill_switch_reflects_active_state(client):
    client.post("/api/kill-switch/enable")
    body = client.get("/").text
    assert 'data-state="active"' in body
    assert "kill switch active" in body


def test_overview_shows_trading_session_card(client):
    body = client.get("/").text
    assert "Trading session" in body
    # One of the documented session labels must render.
    assert any(
        label in body for label in ("Asia", "London", "New York", "Off-hours")
    ), body[:200]


def test_overview_shows_current_session_time(client):
    body = client.get("/").text
    # The current_session_time helper formats as "HH:MM:SS ET".
    assert " ET" in body
    assert "data-session-time" in body


def test_overview_pnl_card_renamed(client):
    body = client.get("/").text
    # New label is just "P&L"; old label was "Paper P&L today".
    assert "Paper P&amp;L" not in body
    assert "P&amp;L" in body


def test_overview_shows_win_rate_and_total_points_cards(client):
    body = client.get("/").text
    assert "Win rate" in body
    assert "Total points %" in body


def test_overview_no_longer_has_separate_open_positions_card(client):
    """The standalone Open Positions card is gone — the broker-account
    card now carries the same info."""
    body = client.get("/").text
    # The old card label was exactly "Open positions" rendered twice
    # (once as its label, once as the position-table header). With the
    # cleanup, the only "Open positions" label is inside the broker-
    # account card.
    assert body.count("Open positions") <= 1


def test_overview_removed_recent_paper_orders_section(client):
    body = client.get("/").text
    assert "Recent paper orders" not in body
    # The Open Orders block (restyled) replaces it.
    assert "Open orders" in body


def test_overview_keeps_per_day_counter_cards(client):
    body = client.get("/").text
    # The cards still filter to armed-mode submissions, but the labels
    # drop the "(Armed)" / "Armed trades" verbiage — armed is a state,
    # not a kind of trade.
    assert "Trades today" in body
    assert "Accepted today" in body
    assert "Rejected today" in body


# ---------------------------------------------------------------------------
# Broker page
# ---------------------------------------------------------------------------


def test_broker_page_heading(client):
    body = client.get("/settings/broker").text
    assert "<h1>Broker Settings</h1>" in body


def test_broker_page_no_paper_broker_card(client):
    body = client.get("/settings/broker").text
    assert "Paper broker" not in body
    # Paper controls block also gone.
    assert "Paper controls" not in body


def test_broker_page_account_selector_is_a_dropdown(client):
    body = client.get("/settings/broker").text
    # Old field was <input ... name="selected_account_id">; new field
    # must be a <select>.
    assert '<select id="selected_account_id" name="selected_account_id"' in body
    assert "data-account-dropdown" in body


def test_broker_page_keeps_test_connection_and_topstep_buttons(client):
    body = client.get("/settings/broker").text
    assert 'id="btn-test"' in body
    assert 'id="btn-topstep-auth"' in body
    assert 'id="btn-topstep-accounts"' in body


# ---------------------------------------------------------------------------
# Risk page
# ---------------------------------------------------------------------------


def test_risk_page_disables_fixed_contracts_when_strategy_managed(client):
    """When strategy_managed_risk is on (the default fixture state) the
    Fixed contracts input is rendered with the `disabled` attr so the
    field is visibly inactive."""
    # The default fixture leaves STRATEGY_MANAGED_RISK at the .env default,
    # which is on per app/config.py. Force it on to be deterministic.
    client.app.state.settings.strategy_managed_risk = True
    body = client.get("/settings/risk").text
    # The fixed contracts input must be rendered disabled.
    assert 'id="fixed_contracts_per_trade"' in body
    # Look for `disabled` near the fixed_contracts input.
    snippet_start = body.index('id="fixed_contracts_per_trade"')
    snippet = body[snippet_start: snippet_start + 400]
    assert "disabled" in snippet


def test_risk_page_enables_fixed_contracts_when_strategy_not_managed(client):
    client.app.state.settings.strategy_managed_risk = False
    body = client.get("/settings/risk").text
    snippet_start = body.index('id="fixed_contracts_per_trade"')
    snippet = body[snippet_start: snippet_start + 400]
    assert "disabled" not in snippet


def test_risk_page_renders_single_symbol_input(client):
    """Post-merge the risk page surfaces a SINGLE allowlist that
    applies in every state — the old armed-only stricter subset was
    confusing operators and is gone."""
    body = client.get("/settings/risk").text
    assert 'name="allowed_symbols"' in body
    assert 'name="allowed_symbols_armed"' not in body
    assert 'id="allowed_symbols_armed"' not in body


def test_risk_post_round_trips_symbols(client):
    """Submitting the form preserves the single ALLOWED_SYMBOLS list."""
    r = client.post(
        "/settings/risk",
        data={
            "max_contracts_per_trade": "3",
            "strategy_managed_risk": "false",
            "fixed_contracts_per_trade": "1",
            "max_daily_loss": "250",
            "max_open_positions": "1",
            "duplicate_order_cooldown_seconds": "60",
            "enable_longs": "true",
            "enable_shorts": "true",
            "allowed_symbols": "MES1!,MNQ1!,NQ1!",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    s = client.app.state.settings
    assert s.allowed_symbols == ["MES1!", "MNQ1!", "NQ1!"]


# ---------------------------------------------------------------------------
# TradingView page
# ---------------------------------------------------------------------------


def test_tradingview_page_has_collapsible_sections(client):
    body = client.get("/tradingview").text
    # After the polish pass: only the Xiznit ORB collapsible survives
    # — the Generic / manual alert JSON template collapsible was
    # removed. At least one collapsible remains, and it must be closed.
    assert body.count('<details class="collapsible"') >= 1
    assert '<details class="collapsible" open' not in body


# NOTE: test_tradingview_alert_template_still_renders removed during
# the polish pass — the Generic / manual alert JSON template
# collapsible was deleted, so there's no alert_template to assert on.


# ---------------------------------------------------------------------------
# Metrics page
# ---------------------------------------------------------------------------


def test_metrics_page_removes_paper_pnl_and_open_position_cards(client):
    body = client.get("/metrics").text
    assert "Paper P&amp;L" not in body
    # The standalone "Open positions" card is removed; the open-position
    # table is no longer rendered on /metrics.
    assert "Open positions" not in body


def test_metrics_page_renders_empty_profit_graph_state(client):
    """No closed trades yet → the empty-state container is shown."""
    body = client.get("/metrics").text
    assert "No profit data yet." in body


def test_metrics_page_renders_profit_graph_when_data_exists(client):
    """When the journal has closed trades, an SVG polyline is rendered."""
    j = client.app.state.journal
    j.record_closed_trade(
        symbol="MES1!", side="long", contracts=1,
        entry_price=5000.0, exit_price=5002.0,
        realized_pnl_points=2.0, broker_provider="paper",
    )
    j.record_closed_trade(
        symbol="MES1!", side="long", contracts=1,
        entry_price=5002.0, exit_price=5001.0,
        realized_pnl_points=-1.0, broker_provider="paper",
    )
    body = client.get("/metrics").text
    assert "<polyline" in body
    assert "No profit data yet." not in body


# ---------------------------------------------------------------------------
# Helper functions in app.dashboard
# ---------------------------------------------------------------------------


def test_current_trading_session_labels():
    # Use a real datetime instance for each window. The helper expects an
    # America/New_York-aware datetime; we just verify the time-of-day
    # mapping by passing naive datetimes — the helper only reads .time().
    dt_ny = datetime(2026, 5, 18, 10, 30)
    assert dashboard_mod.current_trading_session(dt_ny) == "New York"
    dt_london = datetime(2026, 5, 18, 5, 0)
    assert dashboard_mod.current_trading_session(dt_london) == "London"
    dt_asia_late = datetime(2026, 5, 18, 20, 0)
    assert dashboard_mod.current_trading_session(dt_asia_late) == "Asia"
    dt_asia_early = datetime(2026, 5, 18, 1, 0)
    assert dashboard_mod.current_trading_session(dt_asia_early) == "Asia"
    dt_off = datetime(2026, 5, 18, 16, 30)
    assert dashboard_mod.current_trading_session(dt_off) == "Off-hours"


def test_current_session_time_formats():
    dt = datetime(2026, 5, 18, 10, 30, 5)
    s = dashboard_mod.current_session_time(dt)
    assert s == "10:30:05 ET"


def test_win_rate_helper(tmp_path: Path):
    j = Journal(tmp_path / "wr.db")
    assert dashboard_mod.win_rate(j) == "N/A"
    for pts in (1.0, 1.0, -0.5):
        j.record_closed_trade(
            symbol="X", side="long", contracts=1,
            entry_price=1.0, exit_price=1.0 + pts,
            realized_pnl_points=pts, broker_provider="paper",
        )
    rate = dashboard_mod.win_rate(j)
    # 2 / 3 wins = 66.7%
    assert rate == "66.7%"


def test_total_points_percentage_helper(tmp_path: Path):
    j = Journal(tmp_path / "tpp.db")
    assert dashboard_mod.total_points_percentage(j) == "N/A"
    j.record_closed_trade(
        symbol="X", side="long", contracts=1,
        entry_price=1.0, exit_price=2.0,
        realized_pnl_points=1.0, broker_provider="paper",
    )
    j.record_closed_trade(
        symbol="X", side="long", contracts=1,
        entry_price=1.0, exit_price=2.0,
        realized_pnl_points=1.0, broker_provider="paper",
    )
    # net points / trades = (1+1) / 2 = 1.0 → +100.0%
    assert dashboard_mod.total_points_percentage(j) == "+100.0%"


def test_profit_series_helper(tmp_path: Path):
    j = Journal(tmp_path / "ps.db")
    assert dashboard_mod.profit_series(j) == []
    for pts in (1.0, -0.5, 0.25):
        j.record_closed_trade(
            symbol="X", side="long", contracts=1,
            entry_price=1.0, exit_price=1.0 + pts,
            realized_pnl_points=pts, broker_provider="paper",
        )
    series = dashboard_mod.profit_series(j)
    assert [p["points"] for p in series] == [1.0, -0.5, 0.25]
    # cumulative should be running sum.
    cumul = [p["cumulative"] for p in series]
    assert cumul[0] == 1.0
    assert cumul[1] == 0.5
    assert abs(cumul[2] - 0.75) < 1e-6


# ---------------------------------------------------------------------------
# Existing flows still work
# ---------------------------------------------------------------------------


def test_webhook_flow_still_works(client):
    """The whole point of the cleanup is to keep behavior intact."""
    r = client.post("/webhooks/tradingview", json=make_alert(order_id="cleanup_1"))
    assert r.status_code == 200
    assert r.json()["accepted"] is True
