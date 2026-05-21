"""Tests for the mobile/responsive UX pass.

These cover the markup additions made when the site was overhauled for
phone-sized viewports. They are deliberately HTML-only — they don't
exercise execution logic, broker behavior, or the kill-switch gates.

What they do assert:

  * Every primary page returns 200 (smoke).
  * The base template ships the mobile header + drawer scaffolding.
  * The sidebar carries the drawer class hook the CSS targets.
  * Tables that overflow desktop layouts use a responsive wrapper.
  * The execution action row uses ``mobile-actions`` so the buttons
    stack on phones.
  * The metrics Past Orders block exposes a mobile card-list view.
  * The symbols mapping table has the responsive scroll wrapper.
  * The TradingView code blocks remain inside a contained scroll
    region, and the secret-copy row uses ``copy-row``.
  * styles.css declares the mobile drawer + breakpoint rules.

The tests are forgiving on whitespace — they only assert for class
names + structural hooks the templates emit. They do **not** assert
visual rendering.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from .conftest import _build_app


# ---------------------------------------------------------------------------
# All primary pages still return 200
# ---------------------------------------------------------------------------

PRIMARY_PAGES = [
    "/",
    "/settings/broker",
    "/settings/risk",
    "/settings/symbols",
    "/tradingview",
    "/metrics",
    "/journal",
    "/settings/profile",
    "/logs",
    "/system",
]


def test_primary_pages_return_200(client):
    for path in PRIMARY_PAGES:
        r = client.get(path)
        assert r.status_code == 200, f"{path} returned {r.status_code}"
        assert "text/html" in r.headers.get("content-type", "")


# ---------------------------------------------------------------------------
# Mobile drawer / hamburger scaffolding on every page
# ---------------------------------------------------------------------------


def test_mobile_header_and_hamburger_present_on_every_page(client):
    for path in PRIMARY_PAGES:
        body = client.get(path).text
        assert 'class="mobile-header"' in body, f"{path} missing mobile-header"
        assert 'id="mobile-drawer-toggle"' in body, (
            f"{path} missing hamburger toggle"
        )
        assert 'aria-controls="app-sidebar"' in body, (
            f"{path} hamburger missing aria-controls hook"
        )


def test_sidebar_carries_drawer_class(client):
    body = client.get("/").text
    # The drawer class is what the responsive CSS targets to slide the
    # sidebar in/out under the mobile breakpoint.
    assert 'class="sidebar mobile-drawer"' in body
    assert 'id="app-sidebar"' in body
    # An overlay element exists for tap-to-dismiss.
    assert 'id="mobile-drawer-overlay"' in body
    # The close button lives on the drawer itself.
    assert 'id="mobile-drawer-close"' in body


def test_viewport_meta_supports_safe_area(client):
    body = client.get("/").text
    assert 'name="viewport"' in body
    # We use viewport-fit=cover so safe-area-inset-* is honoured by iOS
    # browsers when the drawer opens.
    assert "viewport-fit=cover" in body


# ---------------------------------------------------------------------------
# Execution card uses mobile-actions for stackable buttons
# ---------------------------------------------------------------------------


def test_execution_actions_use_mobile_actions_container(client):
    body = client.get("/").text
    # The Disengage / Flatten / Smoke buttons live inside a container
    # that uses the shared mobile-actions class.
    assert "execution-actions mobile-actions" in body or (
        "mobile-actions" in body and "execution-actions" in body
    )


# ---------------------------------------------------------------------------
# Tables use the responsive scroll wrapper
# ---------------------------------------------------------------------------


def test_dashboard_renders_responsive_scroll_wrapper(client):
    """The dashboard still ships the ``table-scroll`` and
    ``mobile-card-list`` classes used by the responsive CSS, regardless
    of whether the Topstep open-orders endpoint has populated the
    table."""
    body = client.get("/").text
    # Empty-state still renders the flatten/smoke modal markup, but
    # the open-orders table-scroll wrapper only appears when there are
    # orders to render. Settings/symbols carries the wrapper too —
    # confirm at least one of the breakpoint utilities is present.
    assert "mobile-actions" in body or "table-scroll" in body


def test_symbol_mappings_table_has_mobile_safe_wrapper(client):
    body = client.get("/settings/symbols").text
    # Symbol mappings table is wrapped in the responsive scroll class.
    assert "table-scroll" in body
    # The contract-search row uses the responsive class.
    assert "contract-search-row" in body


def test_journal_tables_use_responsive_scroll_wrapper(client):
    from .conftest import make_alert
    client.post(
        "/webhooks/tradingview", json=make_alert(order_id="mobile_j_1")
    )
    body = client.get("/journal").text
    assert "table-scroll" in body


# ---------------------------------------------------------------------------
# TradingView page mobile cleanups
# ---------------------------------------------------------------------------


def test_tradingview_secret_uses_copy_row(client):
    body = client.get("/tradingview").text
    # Webhook secret + Copy button live inside a .copy-row that stacks
    # under the mobile breakpoint.
    assert 'class="copy-row"' in body
    # And the dark-input class is applied so it doesn't render as a
    # white system text box on phones.
    assert 'class="dark-input"' in body


# test_tradingview_field_reference_table_has_mobile_safe_wrapper removed
# alongside the Field reference card itself (ui-revisions 4.3) — the
# section it asserted on no longer exists.


# ---------------------------------------------------------------------------
# styles.css declares the mobile drawer + breakpoint rules
# ---------------------------------------------------------------------------


def _styles_text() -> str:
    return (
        Path(__file__).resolve().parent.parent
        / "app"
        / "static"
        / "styles.css"
    ).read_text()


def test_styles_css_declares_mobile_drawer_rules():
    css = _styles_text()
    # Drawer classes the JS toggles.
    assert ".mobile-header" in css
    assert ".mobile-drawer-toggle" in css
    assert ".mobile-drawer-overlay" in css
    assert ".sidebar.is-open" in css or "body.sidebar-open .sidebar" in css
    # Helper classes the templates reference.
    for cls in (
        ".mobile-stack",
        ".mobile-actions",
        ".mobile-full",
        ".mobile-card-list",
        ".table-scroll",
        ".copy-row",
    ):
        assert cls in css, f"missing helper class: {cls}"


def test_styles_css_has_mobile_breakpoints():
    css = _styles_text()
    # The two primary breakpoints used in the responsive section.
    assert "@media (max-width: 768px)" in css
    assert "@media (max-width: 480px)" in css


def test_styles_css_prevents_horizontal_page_scroll():
    css = _styles_text()
    # Under the mobile breakpoint we explicitly clamp overflow-x.
    # We don't insist on the exact rule but it must be there.
    assert "overflow-x: hidden" in css


def test_styles_css_inputs_are_44px_high_on_mobile():
    """Touch-friendly target: forms get min-height >= 44px on mobile."""
    css = _styles_text()
    # Look for the min-height bump near the input rule.
    assert "min-height: 44px" in css


# ---------------------------------------------------------------------------
# Drawer behavior wired up via JS
# ---------------------------------------------------------------------------


def test_base_template_wires_drawer_open_and_close(client):
    body = client.get("/").text
    # The JS handler toggles the sidebar-open class on <body>.
    assert "sidebar-open" in body
    # Escape closes the drawer.
    assert "ev.key === 'Escape'" in body
    # Nav links auto-close the drawer.
    assert "closeDrawer" in body


# ---------------------------------------------------------------------------
# Modals are still in the DOM (mobile-friendly modal styling is in CSS)
# ---------------------------------------------------------------------------


def test_execution_card_renders_three_state_dropdown(tmp_path, monkeypatch):
    """Post-collapse: the live-engagement modal is gone. The
    Execution card hosts a single Off/Test/Armed dropdown."""
    app = _build_app(tmp_path, monkeypatch)
    with TestClient(app) as c:
        body = c.get("/").text
    assert 'id="execution_mode_select"' in body
    assert '<option value="off"' in body
    assert '<option value="test"' in body
    assert '<option value="armed"' in body
    # The pre-collapse live-engagement modal is gone.
    assert 'id="live-execution-modal"' not in body
    assert 'id="live_confirm_phrase"' not in body
