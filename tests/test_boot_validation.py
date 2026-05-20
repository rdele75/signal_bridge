"""Tests for the boot-time secret validator (finding C2).

The validator runs inside ``create_app()`` and refuses to build the app
when the TradingView webhook secret is missing, the public placeholder,
or shorter than the minimum length. An escape hatch via
``SIGNALBRIDGE_ALLOW_INSECURE_BOOT=1`` downgrades the refusal to a loud
WARNING for debug sessions.
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
):
    """Build a Settings instance with the supplied webhook secret.

    ``webhook_secret=None`` removes the env var so the default applies.
    Returns ``(settings, log)`` for the caller to feed into the
    validator.
    """
    if webhook_secret is None:
        monkeypatch.delenv("TRADINGVIEW_WEBHOOK_SECRET", raising=False)
    else:
        monkeypatch.setenv("TRADINGVIEW_WEBHOOK_SECRET", webhook_secret)

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
