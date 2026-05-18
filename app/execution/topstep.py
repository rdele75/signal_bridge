"""Topstep / TopstepX (ProjectX) broker adapter — read-only account data.

This phase implements everything SignalBridge needs to *observe* the
configured TopstepX account, without ever placing or cancelling an order:

* ``authenticate()``  — POSTs ``/api/Auth/loginKey`` with the configured
  username/API key, caches the JWT, conservatively expires it 23h later.
* ``get_auth_headers()`` — returns ``Authorization: Bearer <token>``,
  authenticating first if the cached token is missing or expired.
* ``get_accounts()`` — POSTs ``/api/Account/search`` with
  ``onlyActiveAccounts=true`` and returns the parsed list.
* ``get_selected_account()`` — picks the account matching the configured
  ``TOPSTEP_ACCOUNT_ID`` / ``SELECTED_ACCOUNT_ID``. Compares ids as
  strings so a numeric ProjectX id (e.g. ``5001``) and the stored
  string form (``"5001"``) match cleanly.
* ``get_positions()`` / ``get_orders()`` — scaffolded read-only calls.
  Endpoint shapes for live Topstep positions / orders are not yet
  pinned down for this build, so both return a structured
  ``status=not_implemented`` envelope with the selected account info
  attached. They must never crash and must never reach a write API.
* ``test_connection()`` — runs auth + account discovery and reports a
  structured envelope including the selected account snapshot,
  ``accounts_count``, ``selected_account_id``, and the masked token
  cache state.

Order routing is intentionally disabled in this phase. Every mutating
method refuses with ``status=topstep_execution_not_enabled`` and
``execute()`` raises ``NotImplementedError`` so the webhook handler
records a clearly labeled rejection instead of silently no-op'ing real
trading.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

import httpx

from ..schemas import ExecutionResult, NormalizedSignal
from .broker_base import BrokerBase


log = logging.getLogger("signalbridge.broker.topstep")

DEFAULT_BASE_URL = "https://api.topstepx.com"
DEFAULT_WS_URL = "https://rtc.topstepx.com"

DEFAULT_TIMEOUT_SECONDS = 15.0
# Topstep doesn't document a hard token TTL on /loginKey; ProjectX guidance
# is to re-auth at least daily. 23h gives us slack ahead of any 24h cutoff.
TOKEN_TTL_HOURS = 23


# Persistence hook signature: (token, iso_expires_at) -> None.
TokenSink = Callable[[str, str], None]


def _has(value: Optional[str]) -> bool:
    return bool(value and str(value).strip())


def _mask_api_key(value: Optional[str]) -> str:
    """Safe-to-display preview of the API key. Never echoes the secret."""
    if not value:
        return ""
    text = str(value)
    if len(text) <= 4:
        return "configured"
    return f"…{text[-4:]}"


def _mask_token(value: Optional[str]) -> str:
    """Safe-to-display preview of the auth token. Never echoes the JWT."""
    if not value:
        return ""
    text = str(value)
    if len(text) <= 4:
        return "configured"
    return f"…{text[-4:]}"


class TopstepBroker(BrokerBase):
    name = "topstep"
    provider = "topstep"
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
        token_sink: Optional[TokenSink] = None,
        http_timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        # Strip credentials on load: stray whitespace in the dashboard /
        # .env was the cause of phantom errorCode=3 responses that worked
        # fine via curl.
        self.username = (username or "").strip()
        # Kept for parity with other adapters even though ProjectX uses an
        # API key, not a password.
        self.password = password or ""
        self.api_key = (api_key or "").strip()
        self.account_id = (account_id or "").strip()
        self.env = (env or "demo").lower()
        self.base_url = (base_url or DEFAULT_BASE_URL).strip().rstrip("/")
        self.ws_url = (ws_url or DEFAULT_WS_URL).strip().rstrip("/")
        self.token = (token or "").strip()
        self.token_expires_at = (token_expires_at or "").strip()
        self._token_sink = token_sink
        self._http_timeout = http_timeout

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
            "token_preview": _mask_token(self.token),
            "token_expires_at": self.token_expires_at or "",
        }

    # ------------------------------------------------------------------
    # HTTP plumbing (overridable in tests)
    # ------------------------------------------------------------------

    def _post_json(
        self, path: str, payload: dict[str, Any], *, auth: bool = False
    ) -> tuple[int, Any]:
        """POST JSON to TopstepX and return ``(status_code, body)``.

        ``body`` is the parsed JSON dict when the response is JSON, the raw
        text otherwise, or a short error string on network failure (with
        status=0). Tests monkey-patch this method to avoid real HTTP.

        ProjectX's loginKey endpoint expects ``accept: text/plain`` (the
        exact shape used in their published Swagger / curl examples).
        Other endpoints take JSON responses.
        """
        url = f"{self.base_url}{path}"
        if path == "/api/Auth/loginKey":
            headers = {
                "accept": "text/plain",
                "Content-Type": "application/json",
            }
        else:
            headers = {
                "accept": "application/json",
                "Content-Type": "application/json",
            }
        if auth:
            bearer = self._auth_headers_or_none()
            if bearer:
                headers.update(bearer)
        try:
            response = httpx.post(
                url,
                json=payload,
                headers=headers,
                timeout=self._http_timeout,
            )
        except httpx.RequestError as exc:
            log.warning(
                "topstep POST %s failed: %s", path, exc.__class__.__name__
            )
            return 0, f"network_error: {exc.__class__.__name__}"
        try:
            return response.status_code, response.json()
        except ValueError:
            return response.status_code, response.text

    # ------------------------------------------------------------------
    # Token cache / persistence
    # ------------------------------------------------------------------

    def _is_token_valid(self) -> bool:
        if not _has(self.token):
            return False
        if not _has(self.token_expires_at):
            return False
        try:
            expires = datetime.fromisoformat(self.token_expires_at)
        except ValueError:
            return False
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) < expires

    def _store_token(self, token: str, expires_at: str) -> None:
        self.token = token
        self.token_expires_at = expires_at
        if self._token_sink is not None:
            try:
                self._token_sink(token, expires_at)
            except Exception as exc:  # pragma: no cover - best-effort persistence
                log.warning(
                    "topstep token persistence failed: %s",
                    exc.__class__.__name__,
                )

    def _auth_headers_or_none(self) -> Optional[dict[str, str]]:
        if not _has(self.token):
            return None
        return {"Authorization": f"Bearer {self.token}"}

    def get_auth_headers(self) -> dict[str, str]:
        """Return the Authorization header, authenticating first if needed.

        Raises ``RuntimeError`` if credentials are missing or auth fails.
        Use ``authenticate()`` directly when you need the structured envelope.
        """
        if not self._is_token_valid():
            auth = self.authenticate()
            if not auth.get("ok"):
                raise RuntimeError(
                    auth.get("message") or "topstep authentication failed"
                )
        headers = self._auth_headers_or_none()
        if headers is None:
            raise RuntimeError("topstep token missing after authenticate()")
        return headers

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def _missing_credentials_envelope(self, **extra: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": False,
            "connected": False,
            "provider": self.provider,
            "status": "missing_credentials",
            "not_implemented": False,
            "message": "Topstep username/API key not configured",
            "credentials": self._credentials_summary(),
        }
        payload.update(extra)
        return payload

    def authenticate(self) -> dict[str, Any]:
        if not self._has_required_credentials():
            return self._missing_credentials_envelope()

        # Belt-and-braces: __init__ already strips, but if a caller mutated
        # the attrs directly we still want to send a clean payload.
        username = (self.username or "").strip()
        api_key = (self.api_key or "").strip()

        log.info(
            "topstep auth request endpoint=%s base_url=%s username=%s "
            "username_len=%d api_key_len=%d",
            "/api/Auth/loginKey",
            self.base_url,
            username,
            len(username),
            len(api_key),
        )

        status, body = self._post_json(
            "/api/Auth/loginKey",
            {"userName": username, "apiKey": api_key},
        )
        if status == 0:
            log.warning("topstep auth network error: %s", body)
            return {
                "ok": False,
                "connected": False,
                "provider": self.provider,
                "status": "network_error",
                "message": (
                    body if isinstance(body, str) else "topstep network error"
                ),
                "credentials": self._credentials_summary(),
            }
        if not isinstance(body, dict):
            log.warning(
                "topstep auth non-JSON response http_status=%d", status
            )
            return {
                "ok": False,
                "connected": False,
                "provider": self.provider,
                "status": "auth_failed",
                "http_status": status,
                "message": f"topstep auth returned non-JSON ({status})",
                "credentials": self._credentials_summary(),
            }

        success_flag = bool(body.get("success"))
        error_code = body.get("errorCode")
        error_message = body.get("errorMessage")
        token = str(body.get("token") or "").strip()

        log.info(
            "topstep auth response http_status=%d success=%s errorCode=%s "
            "errorMessage=%s token_present=%s",
            status,
            success_flag,
            error_code,
            error_message,
            bool(token),
        )

        if status == 200 and success_flag and token:
            expires_at = (
                datetime.now(timezone.utc) + timedelta(hours=TOKEN_TTL_HOURS)
            ).isoformat()
            self._store_token(token, expires_at)
            return {
                "ok": True,
                "connected": True,
                "provider": self.provider,
                "status": "authenticated",
                "http_status": status,
                "message": "Topstep authentication successful",
                "token_expires_at": expires_at,
                "credentials": self._credentials_summary(),
            }

        # ProjectX explicitly said success=false (or HTTP error / empty
        # token). Surface the raw errorCode/errorMessage rather than
        # second-guessing it as "wrong credentials".
        return {
            "ok": False,
            "connected": False,
            "provider": self.provider,
            "status": "auth_failed",
            "http_status": status,
            "error_code": error_code,
            "error_message": error_message,
            "message": (
                str(error_message)
                if error_message
                else f"topstep authentication rejected (errorCode={error_code})"
            ),
            "credentials": self._credentials_summary(),
        }

    def refresh_token(self) -> dict[str, Any]:
        # ProjectX expects re-authentication with the API key rather than a
        # separate refresh-token exchange. Same call.
        return self.authenticate()

    # ------------------------------------------------------------------
    # Account discovery
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_account(raw: dict[str, Any]) -> dict[str, Any]:
        """Project Topstep account JSON into the shape the dashboard expects.

        Both ``id`` and ``account_id`` are returned so JS callers and HTML
        templates can read whichever they're already wired up to. A
        string mirror of the id is also exposed so callers can compare
        against the user-configured account id without worrying about
        int-vs-string mismatches.
        """
        raw_id = raw.get("id")
        return {
            "id": raw_id,
            "account_id": raw_id,
            "id_str": "" if raw_id is None else str(raw_id),
            "name": raw.get("name"),
            "balance": raw.get("balance"),
            "can_trade": raw.get("canTrade"),
            "is_visible": raw.get("isVisible"),
            "raw": raw,
        }

    def get_accounts(self) -> dict[str, Any]:
        if not self._has_required_credentials():
            return self._missing_credentials_envelope(accounts=[])

        if not self._is_token_valid():
            auth = self.authenticate()
            if not auth.get("ok"):
                return {
                    "ok": False,
                    "provider": self.provider,
                    "status": auth.get("status", "auth_failed"),
                    "http_status": auth.get("http_status"),
                    "error_code": auth.get("error_code"),
                    "error_message": auth.get("error_message"),
                    "message": auth.get("message", "topstep auth failed"),
                    "accounts": [],
                    "credentials": self._credentials_summary(),
                }

        status, body = self._post_json(
            "/api/Account/search",
            {"onlyActiveAccounts": True},
            auth=True,
        )
        if status == 0:
            return {
                "ok": False,
                "provider": self.provider,
                "status": "network_error",
                "message": (
                    body
                    if isinstance(body, str)
                    else "topstep accounts request failed"
                ),
                "accounts": [],
                "credentials": self._credentials_summary(),
            }
        if not isinstance(body, dict):
            return {
                "ok": False,
                "provider": self.provider,
                "status": "accounts_failed",
                "http_status": status,
                "message": f"topstep accounts returned non-JSON ({status})",
                "accounts": [],
                "credentials": self._credentials_summary(),
            }
        if status >= 400 or body.get("success") is False:
            return {
                "ok": False,
                "provider": self.provider,
                "status": "accounts_failed",
                "http_status": status,
                "error_code": body.get("errorCode"),
                "error_message": body.get("errorMessage"),
                "message": (
                    str(body.get("errorMessage"))
                    if body.get("errorMessage")
                    else f"topstep accounts request failed ({status})"
                ),
                "accounts": [],
                "credentials": self._credentials_summary(),
            }
        raw_accounts = body.get("accounts")
        if not isinstance(raw_accounts, list):
            raw_accounts = []
        accounts = [
            self._normalize_account(a) if isinstance(a, dict) else {"raw": a}
            for a in raw_accounts
        ]
        return {
            "ok": True,
            "provider": self.provider,
            "status": "ok",
            "message": f"{len(accounts)} active account(s)",
            "accounts": accounts,
            "credentials": self._credentials_summary(),
        }

    @staticmethod
    def _match_account_by_id(
        accounts: list[dict[str, Any]], target: str
    ) -> Optional[dict[str, Any]]:
        """Find an account whose id matches ``target`` (compared as a
        trimmed string).

        ProjectX returns numeric account ids; the configured
        ``TOPSTEP_ACCOUNT_ID`` may be persisted as a string. Comparing
        ``str(acct["id"])`` to the trimmed target makes both shapes
        match without surprises.
        """
        if not target:
            return None
        needle = target.strip()
        if not needle:
            return None
        for acct in accounts:
            raw_id = acct.get("id")
            if raw_id is None:
                continue
            if str(raw_id).strip() == needle:
                return acct
        return None

    def get_selected_account(self) -> dict[str, Any]:
        target = str(self.account_id or "").strip()
        accounts_resp = self.get_accounts()
        accounts = accounts_resp.get("accounts") or []
        if not accounts_resp.get("ok"):
            payload = dict(accounts_resp)
            payload.setdefault("selected_account_id", target or None)
            return payload
        if not target:
            return {
                "ok": False,
                "provider": self.provider,
                "status": "no_selected_account",
                "message": (
                    "No Topstep account selected — set TOPSTEP_ACCOUNT_ID "
                    "(or SELECTED_ACCOUNT_ID) to one of the returned ids"
                ),
                "selected_account_id": None,
                "accounts": accounts,
                "credentials": self._credentials_summary(),
            }
        match = self._match_account_by_id(accounts, target)
        if match is None:
            return {
                "ok": False,
                "provider": self.provider,
                "status": "account_not_found",
                "message": (
                    f"Topstep account id {target!r} not found in active accounts"
                ),
                "selected_account_id": target,
                "accounts": accounts,
                "credentials": self._credentials_summary(),
            }
        return {
            "ok": True,
            "provider": self.provider,
            "status": "ok",
            "message": "selected Topstep account found",
            "selected_account_id": target,
            "account": match,
            "accounts": accounts,
            "credentials": self._credentials_summary(),
        }

    # ------------------------------------------------------------------
    # Probe
    # ------------------------------------------------------------------

    def test_connection(self) -> dict[str, Any]:
        if not self._has_required_credentials():
            return self._missing_credentials_envelope(
                accounts_count=0,
                selected_account_id=self.account_id or None,
                selected_account=None,
            )
        auth = self.authenticate()
        if not auth.get("ok"):
            return {
                "ok": False,
                "connected": False,
                "provider": self.provider,
                "status": auth.get("status", "auth_failed"),
                "http_status": auth.get("http_status"),
                "error_code": auth.get("error_code"),
                "error_message": auth.get("error_message"),
                "message": auth.get("message", "topstep auth failed"),
                "credentials": self._credentials_summary(),
                "accounts_count": 0,
                "selected_account_id": self.account_id or None,
                "selected_account": None,
            }
        accounts_resp = self.get_accounts()
        accounts = accounts_resp.get("accounts") or []
        if not accounts_resp.get("ok"):
            return {
                "ok": False,
                "connected": True,
                "provider": self.provider,
                "status": accounts_resp.get("status", "accounts_failed"),
                "http_status": accounts_resp.get("http_status"),
                "error_code": accounts_resp.get("error_code"),
                "error_message": accounts_resp.get("error_message"),
                "message": accounts_resp.get(
                    "message", "topstep accounts fetch failed"
                ),
                "credentials": self._credentials_summary(),
                "accounts_count": 0,
                "selected_account_id": self.account_id or None,
                "selected_account": None,
            }
        target = str(self.account_id or "").strip()
        selected = self._match_account_by_id(accounts, target) if target else None
        if not accounts:
            return {
                "ok": True,
                "connected": True,
                "provider": self.provider,
                "status": "no_accounts",
                "message": (
                    "Topstep auth ok but no active accounts returned"
                ),
                "credentials": self._credentials_summary(),
                "accounts_count": 0,
                "accounts": [],
                "selected_account_id": self.account_id or None,
                "selected_account": None,
            }
        return {
            "ok": True,
            "connected": True,
            "provider": self.provider,
            "status": "ok",
            "message": (
                f"Topstep connected — {len(accounts)} active account(s)"
            ),
            "credentials": self._credentials_summary(),
            "accounts_count": len(accounts),
            "accounts": accounts,
            "selected_account_id": self.account_id or None,
            "selected_account": selected,
        }

    # ------------------------------------------------------------------
    # Read-only queries (positions / orders) — scaffolded.
    #
    # The exact ProjectX request/response shapes for open positions and
    # working orders are not yet pinned down for this build, so both
    # methods return a structured ``not_implemented`` envelope that
    # still carries the configured account id, credential state, and
    # auth-cache info. Callers (the dashboard, /api/broker/*) get a
    # consistent shape and never crash. The mutating endpoints stay
    # disabled regardless.
    #
    # TODO(topstep): wire ``get_positions`` to
    # ``POST /api/Position/searchOpen`` (body ``{"accountId": <int>}``)
    # and ``get_orders`` to ``POST /api/Order/searchOpen`` once the
    # response schemas are confirmed against a real ProjectX account.
    # ------------------------------------------------------------------

    def _read_only_not_implemented(
        self, op: str, *, container_key: str
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": False,
            "provider": self.provider,
            "status": "not_implemented",
            "not_implemented": True,
            "selected_account_id": self.account_id or None,
            "credentials": self._credentials_summary(),
            "message": (
                f"Topstep {op} endpoint not implemented yet"
            ),
        }
        payload[container_key] = []
        return payload

    def get_positions(self) -> dict[str, Any]:
        return self._read_only_not_implemented(
            "positions", container_key="positions"
        )

    def get_orders(self) -> dict[str, Any]:
        return self._read_only_not_implemented(
            "orders", container_key="orders"
        )

    # ------------------------------------------------------------------
    # Mutating actions — disabled in this phase
    # ------------------------------------------------------------------

    def _execution_disabled_envelope(
        self, op: str, **extra: Any
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": False,
            "provider": self.provider,
            "status": "topstep_execution_not_enabled",
            "not_implemented": True,
            "message": (
                "Topstep auth/account discovery is implemented, but order "
                "submission is disabled."
            ),
        }
        payload.update(extra)
        return payload

    def submit_market_order(self, signal: NormalizedSignal) -> dict[str, Any]:
        return self._execution_disabled_envelope(
            "submit_market_order",
            accepted=False,
            symbol=signal.symbol,
            broker_symbol=signal.broker_symbol,
            action=signal.action,
            contracts=signal.contracts,
        )

    def flatten_position(self, symbol: Optional[str] = None) -> dict[str, Any]:
        return self._execution_disabled_envelope(
            "flatten_position", symbol=symbol
        )

    def cancel_all_orders(self, symbol: Optional[str] = None) -> dict[str, Any]:
        return self._execution_disabled_envelope(
            "cancel_all_orders", symbol=symbol
        )

    def execute(self, signal: NormalizedSignal) -> ExecutionResult:
        # Webhook handler catches this and converts it into a clearly
        # labeled rejection rather than silently no-op'ing a real order.
        raise NotImplementedError(
            "topstep_execution_not_enabled: Topstep auth and account "
            "discovery are wired up, but order routing is intentionally "
            "disabled in this phase. Use BROKER_PROVIDER=paper to execute."
        )
