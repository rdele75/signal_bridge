"""Tests for the boot-time secret validator (findings C2 and C1).

The validator runs inside ``create_app()`` and refuses to build the app
when:

* TRADINGVIEW_WEBHOOK_SECRET is missing, the public placeholder, or
  shorter than ``WEBHOOK_SECRET_MIN_LENGTH`` (always checked).
* SESSION_SECRET is missing, the public placeholder, or shorter than
  ``SESSION_SECRET_MIN_LENGTH`` when admin auth is enabled.

An escape hatch via ``SIGNALBRIDGE_ALLOW_INSECURE_BOOT=1`` downgrades
all of these refusals to a loud WARNING for debug sessions.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest


def _fresh_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    webhook_secret: str | None,
    allow_insecure: bool = False,
    admin_auth_enabled: bool = False,
    session_secret: str | None = "a" * 64,
):
    """Build a Settings instance with the supplied webhook + session secrets.

    ``webhook_secret=None`` removes the env var so the default applies.
    ``session_secret=None`` removes the env var (so the Settings field
    default — the placeholder — applies). The helper defaults to
    ``admin_auth_enabled=False`` so tests focused on webhook validation
    don't have to satisfy the session-secret gate too.
    """
    if webhook_secret is None:
        monkeypatch.delenv("TRADINGVIEW_WEBHOOK_SECRET", raising=False)
    else:
        monkeypatch.setenv("TRADINGVIEW_WEBHOOK_SECRET", webhook_secret)

    if session_secret is None:
        monkeypatch.delenv("SESSION_SECRET", raising=False)
    else:
        monkeypatch.setenv("SESSION_SECRET", session_secret)

    monkeypatch.setenv(
        "ADMIN_AUTH_ENABLED", "true" if admin_auth_enabled else "false"
    )

    if allow_insecure:
        monkeypatch.setenv("SIGNALBRIDGE_ALLOW_INSECURE_BOOT", "1")
    else:
        monkeypatch.delenv("SIGNALBRIDGE_ALLOW_INSECURE_BOOT", raising=False)

    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "sb_boot.db"))
    monkeypatch.setenv("LOG_PATH", str(tmp_path / "sb_boot.log"))

    for mod in [m for m in list(sys.modules) if m.startswith("app")]:
        del sys.modules[mod]

    from app import config as config_mod  # noqa: E402

    config_mod.get_settings.cache_clear()
    return config_mod, config_mod.get_settings()


def test_validate_rejects_unset_secret(tmp_path, monkeypatch):
    config_mod, settings = _fresh_settings(
        tmp_path, monkeypatch, webhook_secret=None
    )
    # An unset env var falls back to the placeholder; force the empty
    # case directly to cover the "unset or empty" branch.
    settings = settings.model_copy(update={"webhook_secret": ""})
    errors = config_mod.validate_secrets(settings)
    assert errors
    assert any("unset or empty" in e for e in errors)


def test_validate_rejects_placeholder_secret(tmp_path, monkeypatch):
    config_mod, _ = _fresh_settings(
        tmp_path,
        monkeypatch,
        webhook_secret=config_module_placeholder(),
    )
    settings = config_mod.get_settings()
    errors = config_mod.validate_secrets(settings)
    assert errors
    assert any("placeholder" in e for e in errors)


def test_validate_rejects_short_secret(tmp_path, monkeypatch):
    config_mod, _ = _fresh_settings(
        tmp_path, monkeypatch, webhook_secret="short"
    )
    settings = config_mod.get_settings()
    errors = config_mod.validate_secrets(settings)
    assert errors
    assert any("shorter than" in e for e in errors)


def test_validate_accepts_good_secret(tmp_path, monkeypatch):
    config_mod, _ = _fresh_settings(
        tmp_path,
        monkeypatch,
        webhook_secret="a" * 32,
    )
    settings = config_mod.get_settings()
    assert config_mod.validate_secrets(settings) == []


def test_enforce_raises_with_bad_secret(tmp_path, monkeypatch):
    config_mod, settings = _fresh_settings(
        tmp_path,
        monkeypatch,
        webhook_secret=config_module_placeholder(),
    )
    with pytest.raises(RuntimeError) as exc:
        config_mod.enforce_boot_validation(settings)
    msg = str(exc.value)
    assert "refuses to start" in msg
    assert "openssl rand -hex 32" in msg


def test_enforce_passes_with_good_secret(tmp_path, monkeypatch):
    config_mod, settings = _fresh_settings(
        tmp_path,
        monkeypatch,
        webhook_secret="a" * 32,
    )
    # Should not raise.
    config_mod.enforce_boot_validation(settings)


def test_escape_hatch_boots_with_warning(tmp_path, monkeypatch, caplog):
    config_mod, settings = _fresh_settings(
        tmp_path,
        monkeypatch,
        webhook_secret=config_module_placeholder(),
        allow_insecure=True,
    )
    log = logging.getLogger("signalbridge.boot_test")
    with caplog.at_level(logging.WARNING, logger=log.name):
        config_mod.enforce_boot_validation(settings, log)
    assert any(
        "SIGNALBRIDGE_ALLOW_INSECURE_BOOT" in record.message
        and record.levelno == logging.WARNING
        for record in caplog.records
    )


def test_create_app_refuses_with_placeholder_secret(tmp_path, monkeypatch):
    """End-to-end: create_app() raises RuntimeError before any routes
    are mounted when the webhook secret is still the placeholder.

    ``app/main.py`` evaluates ``app = create_app()`` at module load, so
    the RuntimeError surfaces during the import — pytest.raises wraps
    the import for that reason.
    """
    placeholder = config_module_placeholder()
    monkeypatch.setenv("TRADINGVIEW_WEBHOOK_SECRET", placeholder)
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "sb_e2e.db"))
    monkeypatch.setenv("LOG_PATH", str(tmp_path / "sb_e2e.log"))
    monkeypatch.delenv("SIGNALBRIDGE_ALLOW_INSECURE_BOOT", raising=False)

    for mod in [m for m in list(sys.modules) if m.startswith("app")]:
        del sys.modules[mod]

    from app import config as config_mod  # noqa: E402

    config_mod.get_settings.cache_clear()

    with pytest.raises(RuntimeError) as exc:
        import app.main  # noqa: F401
    assert "TRADINGVIEW_WEBHOOK_SECRET" in str(exc.value)


def config_module_placeholder() -> str:
    """Helper: import the placeholder constant lazily so tests don't
    cache a stale import across monkeypatch resets."""
    for mod in [m for m in list(sys.modules) if m == "app.config"]:
        # Don't blow away every app.* module here — caller manages that.
        # Just make sure the constant is fresh.
        del sys.modules[mod]
    from app.config import WEBHOOK_SECRET_PLACEHOLDER

    return WEBHOOK_SECRET_PLACEHOLDER


def session_secret_placeholder() -> str:
    for mod in [m for m in list(sys.modules) if m == "app.config"]:
        del sys.modules[mod]
    from app.config import SESSION_SECRET_PLACEHOLDER

    return SESSION_SECRET_PLACEHOLDER


# ---------------------------------------------------------------------
# C1 — SESSION_SECRET checks gated on admin_auth_enabled
# ---------------------------------------------------------------------


def test_session_secret_fatal_when_auth_on_and_unset(tmp_path, monkeypatch):
    config_mod, settings = _fresh_settings(
        tmp_path,
        monkeypatch,
        webhook_secret="a" * 32,
        admin_auth_enabled=True,
        session_secret="",
    )
    errors = config_mod.validate_secrets(settings)
    assert any("SESSION_SECRET is unset" in e for e in errors)


def test_session_secret_fatal_when_auth_on_and_placeholder(tmp_path, monkeypatch):
    config_mod, settings = _fresh_settings(
        tmp_path,
        monkeypatch,
        webhook_secret="a" * 32,
        admin_auth_enabled=True,
        session_secret=session_secret_placeholder(),
    )
    errors = config_mod.validate_secrets(settings)
    assert any(
        "SESSION_SECRET is still the public placeholder" in e
        for e in errors
    )


def test_session_secret_fatal_when_auth_on_and_short(tmp_path, monkeypatch):
    config_mod, settings = _fresh_settings(
        tmp_path,
        monkeypatch,
        webhook_secret="a" * 32,
        admin_auth_enabled=True,
        session_secret="x" * 16,
    )
    errors = config_mod.validate_secrets(settings)
    assert any("SESSION_SECRET is shorter than" in e for e in errors)


def test_session_secret_ok_when_auth_on_and_good(tmp_path, monkeypatch):
    config_mod, settings = _fresh_settings(
        tmp_path,
        monkeypatch,
        webhook_secret="a" * 32,
        admin_auth_enabled=True,
        session_secret="x" * 64,
    )
    assert config_mod.validate_secrets(settings) == []


def test_session_secret_ignored_when_auth_off(tmp_path, monkeypatch):
    """With admin auth off the session secret is unused. Validator must
    not raise even with the placeholder in place."""
    config_mod, settings = _fresh_settings(
        tmp_path,
        monkeypatch,
        webhook_secret="a" * 32,
        admin_auth_enabled=False,
        session_secret=session_secret_placeholder(),
    )
    # No fatal errors despite the placeholder.
    assert config_mod.validate_secrets(settings) == []


def test_session_secret_warns_when_auth_off_and_unset(tmp_path, monkeypatch, caplog):
    """Defensive WARNING so the operator notices before flipping auth on."""
    config_mod, settings = _fresh_settings(
        tmp_path,
        monkeypatch,
        webhook_secret="a" * 32,
        admin_auth_enabled=False,
        session_secret="",
    )
    log = logging.getLogger("signalbridge.boot_test_c1")
    with caplog.at_level(logging.WARNING, logger=log.name):
        config_mod.enforce_boot_validation(settings, log)
    assert any(
        "SESSION_SECRET is unset or placeholder" in record.message
        and record.levelno == logging.WARNING
        for record in caplog.records
    )
