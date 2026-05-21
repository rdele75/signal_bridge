"""Property test — every MANAGED_KEY must be visible or explicitly hidden.

Background
----------
docs/operational_audit_2026-05-21.md Section 1 documents an entire class
of "ghost settings" in SignalBridge: values in
``app.settings_store.MANAGED_KEYS`` that persist to SQLite on first
boot and then have no UI edit surface. Once written to SQLite the
``.env`` value is ignored on subsequent boots (see
``settings_store.initialize_settings_from_env``), so an operator who
edits ``.env`` to change one of these settings sees nothing happen and
has no path to discover why short of opening SQLite directly.

The canonical example, the bug that prompted the audit, is
``LIVE_MAX_CONTRACTS_PER_TRADE``: it caps the number of contracts on
real live/funded orders. It was missing from every rendered template
and from every admin form, so an operator who set
``LIVE_MAX_CONTRACTS_PER_TRADE=11`` in ``.env`` post-install saw their
live signals rejected as ``contracts_above_max`` against a stored cap
of 1 — with no UI surface to fix it.

What this test enforces
-----------------------
For every entry in ``MANAGED_KEYS``, at least one of:

  * the Pydantic field name (snake_case) appears in the rendered HTML
    of a protected admin page, or
  * the uppercase ``MANAGED_KEYS`` name appears in the rendered HTML,
    or
  * a substring from ``EXTRA_TOKENS[key]`` appears in the rendered
    HTML, or
  * the key is listed in ``EXPECTED_UI_INVISIBLE`` with a non-empty
    reason string.

Adding a new key to ``MANAGED_KEYS`` therefore forces either a UI
surface or a deliberate exemption with a documented reason. Phase 2
of the consolidation pass will land surfaces for the currently-
invisible keys; each shipped surface should remove the corresponding
entry from ``EXPECTED_UI_INVISIBLE`` so the test asserts visibility
instead.
"""
from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from app.settings_store import MANAGED_KEYS, _KEY_TO_ATTR

from .conftest import login_as_admin


# Pages an authenticated operator can reach. The combined rendered
# HTML across all of these is what the test searches for visibility
# tokens.
PROTECTED_PAGES = (
    "/",
    "/settings/broker",
    "/settings/risk",
    "/settings/symbols",
    "/settings/profile",
    "/tradingview",
    "/system",
    "/journal",
    "/metrics",
)


# Per-key extra visibility tokens. Add an entry here when a setting is
# surfaced via a human label or display variable rather than its raw
# snake_case field name. Each list entry is a substring; ANY match
# counts.
EXTRA_TOKENS: dict[str, list[str]] = {
    # /system renders the bind address as <dt>host</dt> / <dt>port</dt>
    # (`{{ sys.host }}` / `{{ sys.port }}`). The pydantic field names
    # (``app_host`` / ``app_port``) are not surfaced.
    "APP_HOST": [">host</dt>"],
    "APP_PORT": [">port</dt>"],
    # /settings/profile changes ADMIN_USERNAME via the ``new_username``
    # form input. The pydantic field name ``admin_username`` is not
    # rendered as a name attribute.
    "ADMIN_USERNAME": ['name="new_username"'],
    # /settings/profile changes ADMIN_PASSWORD_HASH via the
    # ``new_password`` form input. The hash itself is opaque and
    # intentionally never echoed back.
    "ADMIN_PASSWORD_HASH": ['name="new_password"'],
}


# Keys that are intentionally not surfaced in the UI as of Phase 1.
# Each entry must explain WHY. When Phase 2 lands a UI surface for one
# of these keys, remove its entry here — the test will then assert
# visibility instead. Reasons cite the operational audit doc so the
# rationale stays connected to the source of truth.
EXPECTED_UI_INVISIBLE: dict[str, str] = {
    # ---- adapter-managed auth cache ----
    # Written by the Topstep adapter after a successful
    # /api/Auth/loginKey call. Operator must never touch these — they
    # exist only to survive restarts.
    "TOPSTEP_TOKEN": (
        "adapter-managed auth token cache; never user-editable"
    ),
    "TOPSTEP_TOKEN_EXPIRES_AT": (
        "adapter-managed auth token expiry; never user-editable"
    ),

    # Phase 2 surfaced LIVE_MAX_CONTRACTS_PER_TRADE,
    # LIVE_ALLOWED_SYMBOLS, LIVE_REQUIRE_KILL_SWITCH_OFF, and
    # ENABLE_TOPSTEP_ORDER_DRY_RUN on /settings/risk (audit Section 1
    # critical 1-3 and the high finding). Their EXPECTED_UI_INVISIBLE
    # entries were removed accordingly — the property test now asserts
    # they appear in rendered HTML.

    # ---- audit Section 1: settings driven by armed-flow endpoints ----
    # The dashboard execution card + live-arming modal are the
    # canonical UI for these. They flip via /api/execution/apply-mode
    # and /api/topstep/{demo,live}-execution/{enable,disable}. Adding
    # direct form inputs would create a second-source-of-truth.
    "ENABLE_TOPSTEP_ORDER_EXECUTION": (
        "set indirectly by /api/execution/apply-mode and the "
        "live-arming flow; no direct UI input by design"
    ),
    "TOPSTEP_EXECUTION_CONFIRM": (
        "set indirectly by the demo / live arming endpoints; "
        "no direct UI input by design"
    ),
    "ENABLE_LIVE_TRADING": (
        "set indirectly by "
        "/api/topstep/live-execution/{enable,disable}; no direct UI "
        "input by design"
    ),
    "LIVE_TRADING_CONFIRM": (
        "set indirectly by /api/topstep/live-execution/enable; "
        "no direct UI input by design"
    ),
    "LIVE_TRADING_ACCOUNT_ACK": (
        "set indirectly by the live-execution arm checkbox; "
        "no direct UI input by design"
    ),

    # ---- audit Section 1 MEDIUM findings ----
    "ALLOWED_SYMBOLS": (
        "form input removed from /settings/risk in the polish pass; "
        "awaiting Phase 2 advanced-settings surface (audit Section 1 "
        "medium finding)"
    ),
    "TOPSTEP_BASE_URL": (
        "removed from /settings/broker form in the polish pass; "
        "awaiting Phase 2 advanced-settings surface (audit Section 1 "
        "medium finding)"
    ),
    "TOPSTEP_WS_URL": (
        "removed from /settings/broker form in the polish pass; "
        "awaiting Phase 2 advanced-settings surface (audit Section 1 "
        "medium finding)"
    ),
    "TOPSTEP_ENV": (
        "Form parameter exists on the broker route but no template "
        "input renders for it — silently rewritten to 'demo' on every "
        "save (audit Section 1 medium finding)"
    ),
    "ORDER_HISTORY_LOOKBACK_DAYS": (
        "default for the /metrics order-history dropdown; not "
        "editable from the UI (audit Section 1 medium finding)"
    ),
    "ORDER_HISTORY_LIMIT": (
        "default for the /metrics order-history page size; not "
        "editable from the UI (audit Section 1 medium finding)"
    ),
    "ENABLE_TOPSTEP_REALTIME": (
        "rendered as a static polling label on /settings/broker; "
        "not editable from the UI (audit Section 1 medium finding)"
    ),
    "TOPSTEP_REALTIME_MODE": (
        "polling vs signalr selector; rendered as a static label on "
        "/settings/broker (audit Section 1 medium finding)"
    ),
    "TOPSTEP_REALTIME_POLL_SECONDS": (
        "polling interval; rendered only inside the label string on "
        "/settings/broker (audit Section 1 medium finding)"
    ),
}


def _render_all_pages(client: TestClient) -> str:
    """Render every protected admin page through an authenticated
    TestClient and return the concatenated HTML body."""
    chunks: list[str] = []
    for path in PROTECTED_PAGES:
        r = client.get(path)
        assert r.status_code == 200, (
            f"page {path} returned {r.status_code}: {r.text[:200]}"
        )
        chunks.append(r.text)
    return "\n".join(chunks)


def _key_is_visible(key: str, html: str) -> bool:
    """True when ``key`` is surfaced in the rendered HTML.

    The snake_case Pydantic field name and the uppercase MANAGED_KEY
    name are matched with word boundaries so a longer key that
    *contains* the target as a substring (e.g. ``LIVE_ALLOWED_SYMBOLS``
    embedding ``ALLOWED_SYMBOLS``) does not falsely advertise the
    shorter key as visible. ``EXTRA_TOKENS`` entries stay as literal
    substring matches — they're typically punctuation-bracketed labels
    like ``>host</dt>`` that benefit from raw substring lookup.
    """
    attr = _KEY_TO_ATTR.get(key)
    for word_token in filter(None, (attr, key)):
        if re.search(rf"\b{re.escape(word_token)}\b", html):
            return True
    for extra in EXTRA_TOKENS.get(key, []):
        if extra and extra in html:
            return True
    return False


@pytest.fixture
def rendered_html(auth_app_env) -> str:
    with TestClient(auth_app_env) as c:
        login_as_admin(c)
        return _render_all_pages(c)


class TestManagedKeysVisibility:
    """Every MANAGED_KEY must be either surfaced in the UI or listed
    in EXPECTED_UI_INVISIBLE with a documented reason.

    See module docstring for the bug class this prevents and the
    operational-audit reference (docs/operational_audit_2026-05-21.md
    Section 1).
    """

    def test_no_unaccounted_managed_keys(self, rendered_html: str) -> None:
        unaccounted: list[str] = []
        for key in MANAGED_KEYS:
            surfaced = _key_is_visible(key, rendered_html)
            invisible = key in EXPECTED_UI_INVISIBLE
            if surfaced and invisible:
                unaccounted.append(
                    f"{key}: surfaced in a rendered template AND listed "
                    "in EXPECTED_UI_INVISIBLE — remove the invisible "
                    "entry so the test asserts the surface."
                )
                continue
            if surfaced or invisible:
                continue
            unaccounted.append(
                f"{key} (pydantic={_KEY_TO_ATTR.get(key, '?')}): not "
                "found in any rendered template and not in "
                "EXPECTED_UI_INVISIBLE. Either add a UI surface "
                "(template input or display) OR add an entry to "
                "EXPECTED_UI_INVISIBLE in this test file with a reason."
            )

        if unaccounted:
            joined = "\n  - ".join(unaccounted)
            pytest.fail(
                f"{len(unaccounted)} unaccounted MANAGED_KEYS — see "
                "docs/operational_audit_2026-05-21.md Section 1 for "
                "the bug class this test prevents:\n  - " + joined
            )

    def test_every_invisible_entry_has_a_reason(self) -> None:
        for key, reason in EXPECTED_UI_INVISIBLE.items():
            assert isinstance(reason, str) and reason.strip(), (
                f"EXPECTED_UI_INVISIBLE[{key!r}] must have a non-empty "
                "reason explaining why the key is intentionally not "
                "surfaced."
            )

    def test_invisible_entries_reference_real_keys(self) -> None:
        for key in EXPECTED_UI_INVISIBLE:
            assert key in MANAGED_KEYS, (
                f"EXPECTED_UI_INVISIBLE references {key!r} which is "
                "not in MANAGED_KEYS — remove the stale entry."
            )

    def test_extra_tokens_entries_reference_real_keys(self) -> None:
        for key in EXTRA_TOKENS:
            assert key in MANAGED_KEYS, (
                f"EXTRA_TOKENS references {key!r} which is not in "
                "MANAGED_KEYS — remove the stale entry."
            )
