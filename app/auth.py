"""Admin authentication for the SignalBridge dashboard.

Protection model:
  * /health and /webhooks/tradingview are intentionally PUBLIC.
    The webhook still requires the TradingView shared secret in the JSON
    body — that check lives in WebhookHandler.
  * Everything else (dashboard pages + admin JSON endpoints) requires a
    signed session cookie issued by POST /login when
    ADMIN_AUTH_ENABLED=true.

Why a single password and not user accounts: SignalBridge is a private,
single-operator local app. We add a password so that exposing the dashboard
over Tailscale Funnel is safe — not to become multi-tenant.

Password storage:
  * ``ADMIN_PASSWORD_HASH`` (PBKDF2-SHA256, persisted in SQLite) is the
    preferred check.
  * ``ADMIN_PASSWORD`` plaintext fallback stays for the first-run case
    where the operator has only configured ``.env``. Once the operator
    saves a new password via the Profile page, the hash takes over.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
from typing import TYPE_CHECKING

from fastapi import HTTPException, Request

if TYPE_CHECKING:
    from .config import Settings


DEFAULT_SESSION_SECRET = "generate_or_require_secret"
DEFAULT_ADMIN_PASSWORD = "change_me_admin_password"

# PBKDF2 parameters — stdlib only so we avoid pulling in passlib/bcrypt.
_PBKDF2_ITERATIONS = 200_000
_PBKDF2_SALT_BYTES = 16
_PBKDF2_HASH_NAME = "sha256"
_PBKDF2_SCHEME = "pbkdf2_sha256"


class LoginRequired(Exception):
    """Raised by page handlers when the visitor isn't logged in.

    main.py registers an exception handler that converts this into a 303
    redirect to /login. API endpoints raise HTTPException(401) instead so
    JS callers get a real error rather than an HTML redirect.
    """

    def __init__(self, next_path: str = "/") -> None:
        self.next_path = next_path
        super().__init__("login required")


def _session_available(request: Request) -> bool:
    # SessionMiddleware sets request.scope["session"]; if it isn't
    # installed (auth disabled), reading request.session raises.
    return "session" in request.scope


def is_admin(request: Request) -> bool:
    """True if the visitor is either auth-disabled or holds a valid session."""
    settings: "Settings" = request.app.state.settings
    if not settings.admin_auth_enabled:
        return True
    if not _session_available(request):
        return False
    return bool(request.session.get("admin"))


def hash_password(password: str) -> str:
    """Hash a password using PBKDF2-SHA256. Returns a string of the form
    ``pbkdf2_sha256$<iters>$<salt-hex>$<hash-hex>`` safe to persist."""
    if not isinstance(password, str) or not password:
        raise ValueError("password must be a non-empty string")
    salt = os.urandom(_PBKDF2_SALT_BYTES)
    dk = hashlib.pbkdf2_hmac(
        _PBKDF2_HASH_NAME,
        password.encode("utf-8"),
        salt,
        _PBKDF2_ITERATIONS,
    )
    return f"{_PBKDF2_SCHEME}${_PBKDF2_ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, hashed: str) -> bool:
    """Constant-time check of ``password`` against a stored hash. Returns
    False on any parse error so a corrupted setting can't bypass auth."""
    if not password or not hashed:
        return False
    try:
        scheme, iters_s, salt_hex, hash_hex = hashed.split("$", 3)
    except ValueError:
        return False
    if scheme != _PBKDF2_SCHEME:
        return False
    try:
        iters = int(iters_s)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except (TypeError, ValueError):
        return False
    if iters <= 0:
        return False
    dk = hashlib.pbkdf2_hmac(
        _PBKDF2_HASH_NAME, password.encode("utf-8"), salt, iters
    )
    return hmac.compare_digest(dk, expected)


def check_credentials(
    settings: "Settings", username: str, password: str
) -> bool:
    """Constant-time username + password compare against configured admin.

    Prefers ``ADMIN_PASSWORD_HASH`` when present; falls back to the
    plaintext ``ADMIN_PASSWORD`` so first-run / env-only installs keep
    working until the operator saves a new password via the Profile page.
    """
    if not username or not password:
        return False
    expected_user = settings.admin_username or ""
    if not expected_user:
        return False
    u_ok = hmac.compare_digest(
        username.encode("utf-8"), expected_user.encode("utf-8")
    )
    if not u_ok:
        return False

    stored_hash = getattr(settings, "admin_password_hash", "") or ""
    if stored_hash:
        return verify_password(password, stored_hash)

    expected_pw = settings.admin_password or ""
    if not expected_pw:
        return False
    return hmac.compare_digest(
        password.encode("utf-8"), expected_pw.encode("utf-8")
    )


def login(request: Request) -> None:
    if _session_available(request):
        request.session["admin"] = True


def logout(request: Request) -> None:
    if _session_available(request):
        request.session.clear()


def require_admin_api(request: Request) -> None:
    """FastAPI dependency for admin JSON endpoints."""
    if not is_admin(request):
        raise HTTPException(status_code=401, detail="authentication required")


def require_admin_page(request: Request) -> None:
    """FastAPI dependency for protected HTML page endpoints. Raises
    LoginRequired so the exception handler can issue a 303 redirect to
    /login?next=<path>."""
    if not is_admin(request):
        raise LoginRequired(next_path=request.url.path)


def warn_if_default_secrets(
    settings: "Settings", log: logging.Logger
) -> None:
    """Loud startup warnings when auth knobs are still on insecure defaults."""
    if not settings.admin_auth_enabled:
        log.warning(
            "admin auth is DISABLED — set ADMIN_AUTH_ENABLED=true before "
            "exposing the dashboard via Tailscale Funnel or any public tunnel"
        )
        return
    if (
        not settings.session_secret
        or settings.session_secret == DEFAULT_SESSION_SECRET
    ):
        log.warning(
            "SESSION_SECRET is missing or set to the default — set a long "
            "random value before exposing the dashboard publicly"
        )
    # Only warn about the env-default password when no hash is configured —
    # once the Profile page has been used to save a new password, the
    # plaintext default is irrelevant.
    stored_hash = getattr(settings, "admin_password_hash", "") or ""
    if not stored_hash and (
        not settings.admin_password
        or settings.admin_password == DEFAULT_ADMIN_PASSWORD
    ):
        log.warning(
            "ADMIN_PASSWORD is still the default — change it before "
            "exposing the dashboard publicly"
        )


def safe_next_path(next_path: str | None) -> str:
    """Limit the post-login redirect target to local paths. Prevents
    open-redirect via /login?next=//evil.example.com."""
    if not next_path:
        return "/"
    if not next_path.startswith("/"):
        return "/"
    if next_path.startswith("//"):
        return "/"
    return next_path
