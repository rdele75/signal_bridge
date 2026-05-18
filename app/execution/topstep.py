"""Topstep / TopstepX (ProjectX) broker adapter — SCAFFOLDED.

Topstep is the primary planned live broker target for SignalBridge.
TopstepX exposes its trading API through ProjectX (REST + WebSocket,
API-key authentication, market/historical data, order routing).

This module is the connection foundation for that future integration:
all interface methods are present and return structured, JSON-friendly
envelopes so the dashboard and `/api/broker/*` endpoints stay safe.
No real authentication or order routing runs here yet — every mutating
operation refuses with a clear "not implemented" message, and
``execute()`` raises ``NotImplementedError`` so the webhook handler
turns it into a labeled rejection rather than silently no-op'ing a
real order.

Status conventions returned by ``test_connection()``:

* ``missing_credentials`` — required username/api-key not configured.
* ``scaffolded_not_connected`` — credentials look configured but the
  real ProjectX auth path is not wired up in this build.
"""
from __future__ import annotations

from typing import Any, Optional

from ..schemas import ExecutionResult, NormalizedSignal
from .broker_base import BrokerBase


DEFAULT_BASE_URL = "https://api.topstepx.com"
DEFAULT_WS_URL = "https://rtc.topstepx.com"


def _has(value: Optional[str]) -> bool:
    return bool(value and str(value).strip())


def _mask_api_key(value: Optional[str]) -> str:
    """Return a safe-to-display preview of the API key.

    Never echoes the full secret. Empty input -> "". Short input is
    treated like "configured" without revealing characters.
    """
    if not value:
        return ""
    text = str(value)
    if len(text) <= 4:
        return "configured"
    return f"…{text[-4:]}"


class TopstepBroker(BrokerBase):
    name = "topstep"
    provider = "topstep"
    # Marked "demo" rather than "live" — the adapter is scaffolded and
    # never advertises a live path that doesn't exist.
    execution_mode = "demo"

    def __init__(
        self,
        *,
        username: str = "",
        password: str = "",
        api_key: str = "",
        account_id: str = "",
        env: str = "demo",
        base_url: str = DEFAULT_BASE_URL,
        ws_url: str = DEFAULT_WS_URL,
        token: str = "",
        token_expires_at: str = "",
    ) -> None:
        self.username = username or ""
        # Password is accepted for parity with other adapters but is not
        # part of the documented ProjectX auth path. Kept here so future
        # password-based flows have a slot.
        self.password = password or ""
        self.api_key = api_key or ""
        self.account_id = account_id or ""
        self.env = (env or "demo").lower()
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.ws_url = (ws_url or DEFAULT_WS_URL).rstrip("/")
        self.token = token or ""
        self.token_expires_at = token_expires_at or ""

    # ------------------------------------------------------------------
    # Credential helpers
    # ------------------------------------------------------------------

    def _has_required_credentials(self) -> bool:
        return _has(self.username) and _has(self.api_key)

    def _credentials_summary(self) -> dict[str, Any]:
        """Masked snapshot of credential state. Safe for JSON / dashboard."""
        return {
            "username_set": _has(self.username),
            "api_key_set": _has(self.api_key),
            "api_key_preview": _mask_api_key(self.api_key),
            "account_id": self.account_id or "",
            "account_id_set": _has(self.account_id),
            "env": self.env,
            "base_url": self.base_url,
            "ws_url": self.ws_url,
            "token_cached": _has(self.token),
            "token_expires_at": self.token_expires_at or "",
        }

    # ------------------------------------------------------------------
    # Connection probe / auth
    # ------------------------------------------------------------------

    def test_connection(self) -> dict[str, Any]:
        creds = self._credentials_summary()
        if not self._has_required_credentials():
            return {
                "ok": False,
                "connected": False,
                "provider": self.provider,
                "status": "missing_credentials",
                "not_implemented": False,
                "message": "Topstep username/API key not configured",
                "credentials": creds,
            }
        return {
            "ok": False,
            "connected": False,
            "provider": self.provider,
            "status": "scaffolded_not_connected",
            "not_implemented": True,
            "message": (
                "Topstep adapter scaffolded; real API auth not implemented yet"
            ),
            "credentials": creds,
        }

    def authenticate(self) -> dict[str, Any]:
        creds = self._credentials_summary()
        if not self._has_required_credentials():
            return {
                "ok": False,
                "provider": self.provider,
                "status": "missing_credentials",
                "not_implemented": False,
                "message": "Topstep username/API key not configured",
                "credentials": creds,
            }
        return {
            "ok": False,
            "provider": self.provider,
            "status": "scaffolded_not_connected",
            "not_implemented": True,
            "message": (
                "Topstep authenticate() not implemented yet — ProjectX auth "
                "flow will land in a follow-up phase"
            ),
            "credentials": creds,
        }

    def refresh_token(self) -> dict[str, Any]:
        return {
            "ok": False,
            "provider": self.provider,
            "status": "scaffolded_not_connected",
            "not_implemented": True,
            "message": (
                "Topstep refresh_token() not implemented yet — no token cache "
                "while the adapter is scaffolded"
            ),
            "credentials": self._credentials_summary(),
        }

    # ------------------------------------------------------------------
    # Read-only queries — never raise, always return JSON envelopes
    # ------------------------------------------------------------------

    def _scaffolded_envelope(self, op: str, **extra: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": False,
            "provider": self.provider,
            "status": "scaffolded_not_connected",
            "not_implemented": True,
            "message": f"Topstep {op} not implemented yet",
            "credentials": self._credentials_summary(),
        }
        payload.update(extra)
        return payload

    def get_accounts(self) -> dict[str, Any]:
        return self._scaffolded_envelope("get_accounts", accounts=[])

    def get_selected_account(self) -> dict[str, Any]:
        return self._scaffolded_envelope(
            "get_selected_account",
            selected_account_id=self.account_id or None,
        )

    def get_positions(self) -> dict[str, Any]:
        return self._scaffolded_envelope("get_positions", positions=[])

    def get_orders(self) -> dict[str, Any]:
        return self._scaffolded_envelope("get_orders", orders=[])

    # ------------------------------------------------------------------
    # Mutating actions — refused while scaffolded
    # ------------------------------------------------------------------

    def submit_market_order(self, signal: NormalizedSignal) -> dict[str, Any]:
        return {
            "ok": False,
            "accepted": False,
            "provider": self.provider,
            "status": "scaffolded_not_connected",
            "not_implemented": True,
            "message": "Topstep order submission not implemented yet",
            "symbol": signal.symbol,
            "broker_symbol": signal.broker_symbol,
            "action": signal.action,
            "contracts": signal.contracts,
        }

    def flatten_position(self, symbol: Optional[str] = None) -> dict[str, Any]:
        return self._scaffolded_envelope("flatten_position", symbol=symbol)

    def cancel_all_orders(self, symbol: Optional[str] = None) -> dict[str, Any]:
        return self._scaffolded_envelope("cancel_all_orders", symbol=symbol)

    # ------------------------------------------------------------------
    # Webhook execute path
    # ------------------------------------------------------------------

    def execute(self, signal: NormalizedSignal) -> ExecutionResult:
        # Webhook handler catches this and converts it into a clearly
        # labeled rejection rather than silently no-op'ing a real order.
        raise NotImplementedError(
            "topstep_execution_not_implemented: Topstep / TopstepX adapter "
            "is scaffolded only. Real ProjectX order routing is a planned "
            "follow-up phase. Use BROKER_PROVIDER=paper to execute signals."
        )
