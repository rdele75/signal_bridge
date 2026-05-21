"""Tests for the Profile settings page + webhook-secret regenerate UX
+ Xiznit wording + sidebar Profile link + Broker collapsible indicator.

These are the visible side-effects of the dashboard usability pass:

* POST /tradingview/secret/regenerate updates the stored secret AND the
  resulting page shows the new secret in a copyable field with a clear
  success message + an updated Xiznit URL.
* /tradingview renders the Xiznit Universal ORB webhook URL with
  ?secret=...&symbol={{ticker}} and the generic JSON template stays
  labeled as generic, not as the Xiznit template.
* The sidebar System group contains a Profile link.
* /settings/profile is auth-gated like the rest of the dashboard.
* Profile updates honor the current password, password confirmation,
  and minimum length.
* After saving a new password, the new value works at /login and the
  old one no longer does.
* The Broker / Execution Selection card carries the "click to expand"
  hint so the collapsible indicator is visible.
"""
from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from .conftest import ADMIN_PASSWORD, login_as_admin


# ---------------------------------------------------------------------------
# Webhook secret regenerate UX
# ---------------------------------------------------------------------------


def test_regenerate_secret_updates_stored_setting(client):
    """The POST endpoint must persist a new value in SQLite *and* mirror
    it onto the live Settings object so the next webhook check uses it."""
    store = client.app.state.settings_store
    before = store.get_setting("TRADINGVIEW_WEBHOOK_SECRET")
    r = client.post("/tradingview/secret/regenerate", follow_redirects=False)
    assert r.status_code == 303
    after = store.get_setting("TRADINGVIEW_WEBHOOK_SECRET")
    assert after and after != before
    assert client.app.state.settings.webhook_secret == after


def test_regenerate_page_shows_new_secret_in_copyable_field(client):
    """After regenerating, /tradingview must render the new secret in a
    readable/copyable field. The user can otherwise not copy it into
    TradingView."""
    client.post("/tradingview/secret/regenerate", follow_redirects=False)
    new_secret = client.app.state.settings.webhook_secret
    body = client.get("/tradingview").text
    # The full new value appears verbatim in a readonly input the
    # operator can copy from.
    assert new_secret in body
    assert 'id="current_secret"' in body
    assert "readonly" in body
    # And a copy button is wired up.
    assert 'id="btn-copy-secret"' in body


def test_regenerate_flash_uses_new_wording(client):
    """The success message tells the operator to update *both* TradingView
    alert webhook URLs — the dashboard owns the two-alert Xiznit setup
    now, so the wording matters."""
    r = client.post("/tradingview/secret/regenerate", follow_redirects=False)
    assert r.status_code == 303
    location = r.headers.get("location", "")
    assert "Webhook+secret+regenerated" in location
    assert "Update+both" in location


def test_regenerate_updates_url_examples(client):
    """Page must show the freshly generated secret embedded in the
    Xiznit webhook URL examples — no stale value."""
    # Capture the previous secret first.
    old_secret = client.app.state.settings.webhook_secret
    client.post("/tradingview/secret/regenerate", follow_redirects=False)
    body = client.get("/tradingview").text
    new_secret = client.app.state.settings.webhook_secret
    assert new_secret != old_secret
    # New secret appears in the URL block; old one does not.
    assert "?secret=" + new_secret in body
    assert old_secret not in body


def test_regenerate_does_not_leak_secret_to_logs(client, tmp_path):
    """The regenerate endpoint must not write the new secret into the
    log file. We only confirm a regeneration happened."""
    log_path = client.app.state.settings.log_abs_path
    # Make sure logging is flushed by triggering at least one log line.
    client.post("/tradingview/secret/regenerate", follow_redirects=False)
    new_secret = client.app.state.settings.webhook_secret
    # Flush handlers.
    import logging

    for h in logging.getLogger("signalbridge").handlers:
        try:
            h.flush()
        except Exception:
            pass
    if not log_path.exists():
        return  # log file was empty — the regen path didn't write at all
    text = log_path.read_text()
    assert new_secret not in text, "webhook secret leaked into log file"


# ---------------------------------------------------------------------------
# Xiznit URL wording on the TradingView page
# ---------------------------------------------------------------------------


def test_tradingview_page_shows_xiznit_alert_blocks(client):
    body = client.get("/tradingview").text
    # Two alert blocks with the strategy-specific placeholders.
    assert "Alert 1 — Entries" in body or "Alert 1 — Entries" in body
    assert "Alert 2 — SL" in body or "Alert 2 — SL" in body
    # The {{strategy.order.alert_message}} and {{strategy.alert_message}}
    # placeholders are documented.
    assert "{{strategy.order.alert_message}}" in body
    assert "{{strategy.alert_message}}" in body


def test_tradingview_page_xiznit_url_uses_query_secret_and_ticker(client):
    body = client.get("/tradingview").text
    # The Xiznit URL embeds the *current* webhook secret and uses
    # symbol={{ticker}} so TradingView fills the symbol in for us.
    secret = client.app.state.settings.webhook_secret
    expected = (
        "/webhooks/tradingview?secret=" + secret + "&amp;symbol={{ticker}}"
    )
    # Jinja/HTML escapes `&` to `&amp;` in attribute and pre text contexts;
    # accept both for robustness.
    assert (
        expected in body
        or "/webhooks/tradingview?secret="
        + secret
        + "&symbol={{ticker}}"
        in body
    )


def test_tradingview_page_no_longer_has_generic_template_block(client):
    """The Generic / manual alert JSON template block was removed in
    the polish pass — the Xiznit strategy template is the only
    documented copy-paste source now."""
    body = client.get("/tradingview").text
    assert "Generic / manual alert" not in body
    assert "Generic/manual alert" not in body
    assert "Not the Xiznit" not in body


# ---------------------------------------------------------------------------
# Sidebar Profile entry
# ---------------------------------------------------------------------------


def test_sidebar_contains_profile_link_under_system(client):
    body = client.get("/").text
    # The Profile link lives under the System group.
    assert 'href="/settings/profile"' in body
    # System group contains Logs/System AND Profile entries.
    system_section_match = re.search(
        r"System</summary>(?:.|\n)*?</details>", body
    )
    assert system_section_match, "could not locate sidebar System group"
    system_section = system_section_match.group(0)
    assert "Profile" in system_section
    assert "Logs" in system_section
    assert "System" in system_section


def test_sidebar_profile_link_active_on_profile_page(client):
    body = client.get("/settings/profile").text
    assert 'href="/settings/profile"' in body
    # The active class is added when the current path matches.
    assert 'class="active"' in body


# ---------------------------------------------------------------------------
# /settings/profile auth gate
# ---------------------------------------------------------------------------


@pytest.fixture
def auth_client(auth_app_env):
    with TestClient(auth_app_env) as c:
        yield c


@pytest.fixture
def logged_in_client(auth_app_env):
    with TestClient(auth_app_env) as c:
        login_as_admin(c)
        yield c


def test_profile_page_requires_auth_when_anonymous(auth_client):
    r = auth_client.get("/settings/profile", follow_redirects=False)
    assert r.status_code == 303
    location = r.headers.get("location", "")
    assert location.startswith("/login")


def test_profile_post_requires_auth_when_anonymous(auth_client):
    r = auth_client.post(
        "/settings/profile",
        data={"current_password": "anything"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    location = r.headers.get("location", "")
    assert location.startswith("/login")


def test_profile_page_returns_200_when_authenticated(logged_in_client):
    r = logged_in_client.get("/settings/profile")
    assert r.status_code == 200
    body = r.text
    assert 'action="/settings/profile"' in body
    assert 'name="current_password"' in body
    assert 'name="new_username"' in body
    assert 'name="new_password"' in body
    assert 'name="confirm_password"' in body


def test_profile_page_does_not_render_current_password(logged_in_client):
    """The current password must never be echoed back to the page."""
    body = logged_in_client.get("/settings/profile").text
    assert ADMIN_PASSWORD not in body


# ---------------------------------------------------------------------------
# Profile update flows
# ---------------------------------------------------------------------------


NEW_PASSWORD = "new-admin-password-1"
SHORT_PASSWORD = "short1"  # < 10 chars


def test_profile_update_rejects_wrong_current_password(logged_in_client):
    # Login already migrated the env-default plaintext to a hash —
    # snapshot it so we can prove the failed update did not modify it.
    settings = logged_in_client.app.state.settings
    hash_before = settings.admin_password_hash
    r = logged_in_client.post(
        "/settings/profile",
        data={
            "current_password": "definitely-wrong",
            "new_username": "admin",
            "new_password": NEW_PASSWORD,
            "confirm_password": NEW_PASSWORD,
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    location = r.headers.get("location", "")
    assert "flash_kind=error" in location
    # The hash must not have changed.
    assert settings.admin_password_hash == hash_before


def test_profile_update_rejects_mismatched_new_passwords(logged_in_client):
    settings = logged_in_client.app.state.settings
    hash_before = settings.admin_password_hash
    r = logged_in_client.post(
        "/settings/profile",
        data={
            "current_password": ADMIN_PASSWORD,
            "new_username": "admin",
            "new_password": NEW_PASSWORD,
            "confirm_password": NEW_PASSWORD + "x",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    location = r.headers.get("location", "")
    assert "flash_kind=error" in location
    assert "match" in location.lower()
    assert settings.admin_password_hash == hash_before


def test_profile_update_rejects_short_new_password(logged_in_client):
    settings = logged_in_client.app.state.settings
    hash_before = settings.admin_password_hash
    r = logged_in_client.post(
        "/settings/profile",
        data={
            "current_password": ADMIN_PASSWORD,
            "new_username": "admin",
            "new_password": SHORT_PASSWORD,
            "confirm_password": SHORT_PASSWORD,
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    location = r.headers.get("location", "")
    assert "flash_kind=error" in location
    assert settings.admin_password_hash == hash_before


def test_profile_update_rejects_empty_username(logged_in_client):
    r = logged_in_client.post(
        "/settings/profile",
        data={
            "current_password": ADMIN_PASSWORD,
            "new_username": "",
            "new_password": NEW_PASSWORD,
            "confirm_password": NEW_PASSWORD,
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    location = r.headers.get("location", "")
    assert "flash_kind=error" in location


def test_profile_update_accepts_valid_change(logged_in_client):
    r = logged_in_client.post(
        "/settings/profile",
        data={
            "current_password": ADMIN_PASSWORD,
            "new_username": "admin",
            "new_password": NEW_PASSWORD,
            "confirm_password": NEW_PASSWORD,
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    location = r.headers.get("location", "")
    assert "Profile+updated" in location
    settings = logged_in_client.app.state.settings
    assert settings.admin_password_hash, "password hash must be saved"
    # SQLite mirror.
    stored = logged_in_client.app.state.settings_store.get_setting(
        "ADMIN_PASSWORD_HASH"
    )
    assert stored == settings.admin_password_hash


def test_login_works_with_new_password_after_profile_update(logged_in_client):
    # First, update the password.
    r = logged_in_client.post(
        "/settings/profile",
        data={
            "current_password": ADMIN_PASSWORD,
            "new_username": "admin",
            "new_password": NEW_PASSWORD,
            "confirm_password": NEW_PASSWORD,
        },
        follow_redirects=False,
    )
    assert r.status_code == 303

    # Drop the existing session by logging out.
    logged_in_client.post("/logout", follow_redirects=False)

    # New password authenticates.
    r2 = logged_in_client.post(
        "/login",
        data={"username": "admin", "password": NEW_PASSWORD},
        follow_redirects=False,
    )
    assert r2.status_code == 303
    assert r2.headers.get("location") == "/"


def test_old_password_rejected_after_profile_update(logged_in_client):
    logged_in_client.post(
        "/settings/profile",
        data={
            "current_password": ADMIN_PASSWORD,
            "new_username": "admin",
            "new_password": NEW_PASSWORD,
            "confirm_password": NEW_PASSWORD,
        },
        follow_redirects=False,
    )
    logged_in_client.post("/logout", follow_redirects=False)

    r = logged_in_client.post(
        "/login",
        data={"username": "admin", "password": ADMIN_PASSWORD},
        follow_redirects=False,
    )
    assert r.status_code == 303
    location = r.headers.get("location", "")
    assert "error=invalid" in location


def test_profile_update_username_only_does_not_change_password(
    logged_in_client,
):
    """Submitting the form with new username but blank password fields
    must change the username only and leave authentication intact."""
    r = logged_in_client.post(
        "/settings/profile",
        data={
            "current_password": ADMIN_PASSWORD,
            "new_username": "operator",
            "new_password": "",
            "confirm_password": "",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "Profile+updated" in r.headers.get("location", "")
    settings = logged_in_client.app.state.settings
    assert settings.admin_username == "operator"
    # Login migration writes a hash for the env plaintext, but the
    # username-only path must not re-hash a different password.
    hash_after = settings.admin_password_hash
    from app.auth import verify_password
    assert verify_password(ADMIN_PASSWORD, hash_after) is True
    assert verify_password(NEW_PASSWORD, hash_after) is False

    # New username still authenticates with the existing password.
    logged_in_client.post("/logout", follow_redirects=False)
    r = logged_in_client.post(
        "/login",
        data={"username": "operator", "password": ADMIN_PASSWORD},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers.get("location") == "/"


def test_password_hash_helpers_roundtrip():
    from app.auth import hash_password, verify_password

    h = hash_password("a-very-strong-password")
    assert h.startswith("pbkdf2_sha256$")
    assert verify_password("a-very-strong-password", h) is True
    assert verify_password("a-very-strong-password-wrong", h) is False
    assert verify_password("", h) is False
    assert verify_password("x", "") is False
    assert verify_password("x", "garbage$value") is False


# ---------------------------------------------------------------------------
# Login-time plaintext-to-hash migration
# ---------------------------------------------------------------------------


def test_login_migrates_plaintext_to_hash_on_first_success(auth_client):
    """First successful login against the env-default plaintext must
    persist a PBKDF2 hash so the plaintext is no longer the source of
    truth."""
    settings = auth_client.app.state.settings
    store = auth_client.app.state.settings_store
    assert not settings.admin_password_hash
    assert not store.get_setting("ADMIN_PASSWORD_HASH")

    login_as_admin(auth_client)

    assert settings.admin_password_hash, "hash must be persisted after login"
    stored = store.get_setting("ADMIN_PASSWORD_HASH")
    assert stored == settings.admin_password_hash
    # In-memory plaintext is cleared so check_credentials cannot fall
    # back to it on the next request.
    assert settings.admin_password == ""


def test_login_works_with_existing_plaintext_fallback(auth_client):
    """Before any hash exists, the plaintext env value still allows
    login (the migration path)."""
    settings = auth_client.app.state.settings
    assert not settings.admin_password_hash
    r = auth_client.post(
        "/login",
        data={"username": "admin", "password": ADMIN_PASSWORD},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers.get("location") == "/"


def test_login_works_with_hash_after_migration(auth_client):
    """After migration the plaintext is gone in-memory but the hash
    must continue to accept the original password."""
    login_as_admin(auth_client)
    # Drop the session and try again — should still authenticate.
    auth_client.post("/logout", follow_redirects=False)
    r = auth_client.post(
        "/login",
        data={"username": "admin", "password": ADMIN_PASSWORD},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers.get("location") == "/"


def test_wrong_password_fails_with_and_without_hash(auth_client):
    """Bad password rejected before and after migration."""
    r1 = auth_client.post(
        "/login",
        data={"username": "admin", "password": "wrong-password"},
        follow_redirects=False,
    )
    assert r1.status_code == 303
    assert "error=invalid" in r1.headers.get("location", "")
    # Migrate.
    login_as_admin(auth_client)
    auth_client.post("/logout", follow_redirects=False)
    r2 = auth_client.post(
        "/login",
        data={"username": "admin", "password": "wrong-password"},
        follow_redirects=False,
    )
    assert r2.status_code == 303
    assert "error=invalid" in r2.headers.get("location", "")


def test_password_hash_not_rendered_in_profile_page(logged_in_client):
    settings = logged_in_client.app.state.settings
    body = logged_in_client.get("/settings/profile").text
    # Login already migrated to a hash; verify we don't echo it back.
    assert settings.admin_password_hash, "fixture should have migrated"
    assert settings.admin_password_hash not in body


def test_password_change_does_not_leak_hash_or_password_to_logs(
    logged_in_client,
):
    """The profile update path must not write the new password or its
    hash to the application log."""
    import logging

    settings = logged_in_client.app.state.settings
    log_path = settings.log_abs_path
    new_pw = "another-very-long-password"
    logged_in_client.post(
        "/settings/profile",
        data={
            "current_password": ADMIN_PASSWORD,
            "new_username": "admin",
            "new_password": new_pw,
            "confirm_password": new_pw,
        },
        follow_redirects=False,
    )
    for h in logging.getLogger("signalbridge").handlers:
        try:
            h.flush()
        except Exception:
            pass
    if not log_path.exists():
        return
    text = log_path.read_text()
    assert new_pw not in text, "new password leaked into log"
    assert settings.admin_password_hash not in text, "hash leaked into log"


# ---------------------------------------------------------------------------
# Broker page collapsible indicator
# ---------------------------------------------------------------------------


def test_broker_page_has_collapsible_indicator(client):
    """The Topstep credentials card must remain collapsible with a
    visible affordance."""
    body = client.get("/settings/broker").text
    assert '<details class="collapsible"' in body
    summary_block = re.search(
        r"<summary[^>]*>\s*<h3>Topstep account &amp; credentials</h3>.*?</summary>",
        body,
        re.DOTALL,
    )
    assert summary_block, "Topstep credentials summary not found"
    assert "click to expand" in summary_block.group(0)
