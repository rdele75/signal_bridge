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
"""
from __future__ import annotations

import hmac
import logging
from typing import TYPE_CHECKING

from fastapi import HTTPException, Request

if TYPE_CHECKING:
    from .config import Settings


DEFAULT_SESSION_SECRET = "generate_or_require_secret"
DEFAULT_ADMIN_PASSWORD = "change_me_admin_password"


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


def check_credentials(
    settings: "Settings", username: str, password: str
) -> bool:
    """Constant-time username + password compare against configured admin."""
    if not username or not password:
        return False
    expected_user = settings.admin_username or ""
    expected_pw = settings.admin_password or ""
    if not expected_user or not expected_pw:
        return False
    u_ok = hmac.compare_digest(
        username.encode("utf-8"), expected_user.encode("utf-8")
    )
    p_ok = hmac.compare_digest(
        password.encode("utf-8"), expected_pw.encode("utf-8")
    )
    return u_ok and p_ok


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
    if (
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
