"""Topstep / TopstepX (ProjectX) broker adapter.

This adapter implements:

* ``authenticate()`` / ``get_auth_headers()`` / ``refresh_token()`` —
  ``/api/Auth/loginKey`` + JWT cache (23h TTL).
* ``get_accounts()`` / ``get_selected_account()`` —
  ``/api/Account/search`` with ``onlyActiveAccounts=true``. Account ids
  compare as trimmed strings so ProjectX's numeric ids and the
  user-saved string form match cleanly.
* ``get_positions()`` — ``/api/Position/searchOpen`` (Phase 1).
* ``get_orders()`` — ``/api/Order/searchOpen`` (Phase 1).
* ``search_orders(startTimestamp, endTimestamp)`` —
  ``/api/Order/search`` (Phase 1).
* ``submit_market_order()`` — builds a payload via
  ``topstep_order_builder.build_market_order_payload`` and conditionally
  POSTs ``/api/Order/place`` (Phase 3).

Order routing safety is layered (defense in depth):

  1. ``ENABLE_TOPSTEP_ORDER_EXECUTION`` must be true.
  2. ``EXECUTION_MODE`` must be ``demo`` (never ``live``).
  3. ``TOPSTEP_EXECUTION_CONFIRM`` must be ``DEMO_ONLY``.
  4. ``ENABLE_LIVE_TRADING`` must be false (the hard kill).
  5. The selected account must be numeric and (if known) ``canTrade``.

If any of those gates is open, ``submit_market_order`` refuses with a
structured envelope and never touches the wire. Dry-run mode (the
default) goes through the same builder but stops short of POSTing.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

import httpx

from ..schemas import ExecutionResult, NormalizedSignal
from .broker_base import BrokerBase
from .topstep_order_builder import build_market_order_payload


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
        # Phase 2/3 safety: nothing here flips on order execution by
        # itself. The settings layer + webhook handler are still
        # authoritative; the broker holds them so admin endpoints and
        # programmatic callers can interrogate them without re-reading
        # settings.
        enable_order_execution: bool = False,
        enable_order_dry_run: bool = True,
        execution_confirm: str = "disabled",
        enable_live_trading: bool = False,
        execution_mode: str = "demo",
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
        self.enable_order_execution = bool(enable_order_execution)
        self.enable_order_dry_run = bool(enable_order_dry_run)
        self.execution_confirm = (execution_confirm or "disabled").strip()
        self.enable_live_trading = bool(enable_live_trading)
        # Adapter-level execution mode (paper/demo/live). The webhook
        # layer's settings.execution_mode wins on conflict but this is
        # used for ExecutionResult.broker / .execution_mode labelling
        # and as a final safety check below.
        self.execution_mode = (execution_mode or "demo").lower()

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
    # Read-only queries (positions / orders / order search)
    #
    # All three POST a JSON body containing ``accountId`` (numeric
    # ProjectX id). They never mutate state and never reach a write
    # API. Each returns a structured envelope so the dashboard and
    # /api/broker/* endpoints can render uniformly.
    # ------------------------------------------------------------------

    def _numeric_account_id(self) -> Optional[int]:
        """Return ``int(account_id)`` when it parses, else ``None``.

        ProjectX position/order search endpoints expect a numeric
        ``accountId``. The user-saved id is stored as a string (see
        ``settings_store``), so we coerce here and surface a
        ``non_numeric_account_id`` envelope when the value isn't usable.
        """
        target = str(self.account_id or "").strip()
        if not target:
            return None
        try:
            return int(target)
        except ValueError:
            return None

    def _read_only_envelope_setup(
        self, *, container_key: str
    ) -> tuple[Optional[dict[str, Any]], Optional[int]]:
        """Shared prelude for the read-only POSTs.

        Returns ``(early_return, account_id)``. When ``early_return`` is
        not ``None`` the caller should return it verbatim — credentials
        are missing, auth failed, or the account id isn't numeric. When
        ``early_return`` is ``None`` the second element is the numeric
        accountId ready to be sent in the request body.
        """
        if not self._has_required_credentials():
            payload = self._missing_credentials_envelope()
            payload[container_key] = []
            return payload, None

        numeric_id = self._numeric_account_id()
        if numeric_id is None:
            return (
                {
                    "ok": False,
                    "provider": self.provider,
                    "status": "non_numeric_account_id",
                    "not_implemented": False,
                    "selected_account_id": self.account_id or None,
                    "credentials": self._credentials_summary(),
                    "message": (
                        "Topstep account id is not numeric — set "
                        "TOPSTEP_ACCOUNT_ID to the ProjectX numeric id "
                        "returned by /api/Account/search"
                    ),
                    container_key: [],
                },
                None,
            )

        if not self._is_token_valid():
            auth = self.authenticate()
            if not auth.get("ok"):
                return (
                    {
                        "ok": False,
                        "provider": self.provider,
                        "status": auth.get("status", "auth_failed"),
                        "http_status": auth.get("http_status"),
                        "error_code": auth.get("error_code"),
                        "error_message": auth.get("error_message"),
                        "message": auth.get(
                            "message", "topstep auth failed"
                        ),
                        "selected_account_id": self.account_id or None,
                        "credentials": self._credentials_summary(),
                        container_key: [],
                    },
                    None,
                )

        return None, numeric_id

    def _post_read_only(
        self,
        path: str,
        body: dict[str, Any],
        *,
        container_key: str,
        response_key: str,
        op_label: str,
    ) -> dict[str, Any]:
        status, response = self._post_json(path, body, auth=True)
        if status == 0:
            return {
                "ok": False,
                "provider": self.provider,
                "status": "network_error",
                "selected_account_id": self.account_id or None,
                "credentials": self._credentials_summary(),
                "message": (
                    response
                    if isinstance(response, str)
                    else f"topstep {op_label} request failed"
                ),
                container_key: [],
            }
        if not isinstance(response, dict):
            return {
                "ok": False,
                "provider": self.provider,
                "status": f"{op_label}_failed",
                "http_status": status,
                "selected_account_id": self.account_id or None,
                "credentials": self._credentials_summary(),
                "message": (
                    f"topstep {op_label} returned non-JSON ({status})"
                ),
                container_key: [],
            }
        if status >= 400 or response.get("success") is False:
            return {
                "ok": False,
                "provider": self.provider,
                "status": f"{op_label}_failed",
                "http_status": status,
                "error_code": response.get("errorCode"),
                "error_message": response.get("errorMessage"),
                "selected_account_id": self.account_id or None,
                "credentials": self._credentials_summary(),
                "message": (
                    str(response.get("errorMessage"))
                    if response.get("errorMessage")
                    else f"topstep {op_label} request failed ({status})"
                ),
                container_key: [],
            }
        raw = response.get(response_key)
        if not isinstance(raw, list):
            raw = []
        return {
            "ok": True,
            "provider": self.provider,
            "status": "ok",
            "http_status": status,
            "selected_account_id": self.account_id or None,
            "credentials": self._credentials_summary(),
            "message": f"{len(raw)} {op_label}",
            container_key: raw,
        }

    def get_positions(self) -> dict[str, Any]:
        early, numeric_id = self._read_only_envelope_setup(
            container_key="positions"
        )
        if early is not None:
            return early
        return self._post_read_only(
            "/api/Position/searchOpen",
            {"accountId": numeric_id},
            container_key="positions",
            response_key="positions",
            op_label="positions",
        )

    def get_orders(self) -> dict[str, Any]:
        early, numeric_id = self._read_only_envelope_setup(
            container_key="orders"
        )
        if early is not None:
            return early
        return self._post_read_only(
            "/api/Order/searchOpen",
            {"accountId": numeric_id},
            container_key="orders",
            response_key="orders",
            op_label="orders",
        )

    def search_orders(
        self,
        *,
        start_timestamp: Optional[str] = None,
        end_timestamp: Optional[str] = None,
    ) -> dict[str, Any]:
        """POST ``/api/Order/search`` with an optional time window.

        ProjectX accepts ``startTimestamp`` / ``endTimestamp`` as ISO-8601
        strings. Both are optional — when neither is supplied we send
        just the account id and let the broker pick its default window.
        """
        early, numeric_id = self._read_only_envelope_setup(
            container_key="orders"
        )
        if early is not None:
            return early
        body: dict[str, Any] = {"accountId": numeric_id}
        if start_timestamp:
            body["startTimestamp"] = start_timestamp
        if end_timestamp:
            body["endTimestamp"] = end_timestamp
        return self._post_read_only(
            "/api/Order/search",
            body,
            container_key="orders",
            response_key="orders",
            op_label="search_orders",
        )

    # ------------------------------------------------------------------
    # Mutating actions
    #
    # ``submit_market_order`` is the only routed write API. Bracket
    # orders, flatten, and cancel stay disabled in this build.
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
                "Topstep order submission is disabled by configuration."
            ),
        }
        payload.update(extra)
        return payload

    def _safety_state(self) -> dict[str, Any]:
        """Snapshot of every safety gate. Safe to expose to admins —
        contains no secrets."""
        return {
            "broker_provider": self.provider,
            "execution_mode": self.execution_mode,
            "enable_order_execution": self.enable_order_execution,
            "enable_order_dry_run": self.enable_order_dry_run,
            "execution_confirm": self.execution_confirm,
            "enable_live_trading": self.enable_live_trading,
            "selected_account_id": self.account_id or None,
        }

    def _execution_safety_check(self) -> Optional[str]:
        """Return ``None`` when every gate is satisfied, else the
        identifier of the first failing gate. The caller turns the
        identifier into a labelled envelope.

        Order is deliberate: ``topstep_execution_disabled`` is reported
        first when execution simply isn't on yet so the operator gets
        an actionable label instead of a less-informative
        ``execution_mode_not_demo`` for the mode the build defaults to.
        ``live_execution_locked`` and ``EXECUTION_MODE=live`` still
        short-circuit — the kill switch always wins.
        """
        if self.enable_live_trading:
            return "live_execution_locked"
        if self.execution_mode == "live":
            return "live_execution_locked"
        if not self.enable_order_execution:
            return "topstep_execution_disabled"
        if self.execution_mode != "demo":
            return "execution_mode_not_demo"
        if self.execution_confirm != "DEMO_ONLY":
            return "topstep_execution_confirm_missing"
        if not self._has_required_credentials():
            return "missing_credentials"
        if self._numeric_account_id() is None:
            return "non_numeric_account_id"
        return None

    def build_order_preview(
        self,
        signal: NormalizedSignal,
        *,
        symbol_map: Any = None,
    ) -> dict[str, Any]:
        """Build a dry-run market-order preview for ``signal``.

        Pure: does not authenticate, does not touch the network. Used
        by ``/api/topstep/build-order-preview`` and by the webhook
        handler's dry-run path.
        """
        result = build_market_order_payload(
            signal,
            account_id=self.account_id,
            symbol_map=symbol_map,
            provider=self.provider,
        )
        result.setdefault("provider", self.provider)
        result.setdefault("selected_account_id", self.account_id or None)
        result.setdefault("execution_mode", self.execution_mode)
        return result

    def submit_market_order(
        self,
        signal: NormalizedSignal,
        *,
        symbol_map: Any = None,
    ) -> dict[str, Any]:
        """Submit a market order via ``POST /api/Order/place``.

        Returns ``{"ok": True, "accepted": True, ...}`` on a successful
        ProjectX submission (HTTP 200 + ``success=true`` + ``orderId``).
        Refuses with a structured envelope if any safety gate is open,
        the order builder rejects, or ProjectX rejects.

        No paper fallback: a refusal here surfaces clearly back to the
        webhook handler / admin endpoint instead of silently no-op'ing.
        """
        safety_gate = self._execution_safety_check()
        if safety_gate is not None:
            envelope = self._execution_disabled_envelope(
                "submit_market_order",
                accepted=False,
                status=safety_gate,
                symbol=signal.symbol,
                broker_symbol=signal.broker_symbol,
                action=signal.action,
                contracts=signal.contracts,
                safety=self._safety_state(),
            )
            envelope["message"] = (
                f"Topstep order submission refused: {safety_gate}"
            )
            envelope["would_submit"] = False
            return envelope

        built = self.build_order_preview(signal, symbol_map=symbol_map)
        if not built.get("ok"):
            envelope = dict(built)
            envelope.update(
                {
                    "ok": False,
                    "accepted": False,
                    "status": built.get("reason", "order_build_failed"),
                    "provider": self.provider,
                    "would_submit": False,
                    "safety": self._safety_state(),
                }
            )
            envelope.setdefault(
                "message",
                f"Topstep order build failed: {built.get('reason')}",
            )
            return envelope

        if not self._is_token_valid():
            auth = self.authenticate()
            if not auth.get("ok"):
                return {
                    "ok": False,
                    "accepted": False,
                    "status": auth.get("status", "auth_failed"),
                    "provider": self.provider,
                    "http_status": auth.get("http_status"),
                    "error_code": auth.get("error_code"),
                    "error_message": auth.get("error_message"),
                    "message": auth.get(
                        "message", "topstep auth failed"
                    ),
                    "would_submit": False,
                    "safety": self._safety_state(),
                }

        payload = built["payload"]
        http_status, response = self._post_json(
            "/api/Order/place", payload, auth=True
        )
        if http_status == 0:
            return {
                "ok": False,
                "accepted": False,
                "status": "network_error",
                "provider": self.provider,
                "would_submit": True,
                "message": (
                    response
                    if isinstance(response, str)
                    else "topstep /api/Order/place network error"
                ),
                "payload": payload,
                "safety": self._safety_state(),
            }
        if not isinstance(response, dict):
            return {
                "ok": False,
                "accepted": False,
                "status": "submit_failed",
                "provider": self.provider,
                "http_status": http_status,
                "would_submit": True,
                "message": (
                    f"topstep /api/Order/place returned non-JSON ({http_status})"
                ),
                "payload": payload,
                "safety": self._safety_state(),
            }

        success_flag = bool(response.get("success"))
        order_id = response.get("orderId")
        error_code = response.get("errorCode")
        error_message = response.get("errorMessage")

        log.info(
            "topstep order place http_status=%d success=%s order_id_present=%s "
            "errorCode=%s",
            http_status,
            success_flag,
            order_id is not None,
            error_code,
        )

        if http_status == 200 and success_flag and order_id is not None:
            return {
                "ok": True,
                "accepted": True,
                "status": "submitted",
                "provider": self.provider,
                "http_status": http_status,
                "broker_order_id": str(order_id),
                "order_id": str(order_id),
                "message": "topstep demo order submitted",
                "payload": payload,
                "safety": self._safety_state(),
                "response": {
                    "success": True,
                    "orderId": order_id,
                    "errorCode": error_code,
                    "errorMessage": error_message,
                },
            }

        return {
            "ok": False,
            "accepted": False,
            "status": "submit_rejected",
            "provider": self.provider,
            "http_status": http_status,
            "error_code": error_code,
            "error_message": error_message,
            "message": (
                str(error_message)
                if error_message
                else f"topstep order rejected (errorCode={error_code})"
            ),
            "would_submit": True,
            "payload": payload,
            "safety": self._safety_state(),
            "response": {
                "success": success_flag,
                "orderId": order_id,
                "errorCode": error_code,
                "errorMessage": error_message,
            },
        }

    def flatten_position(self, symbol: Optional[str] = None) -> dict[str, Any]:
        return self._execution_disabled_envelope(
            "flatten_position", symbol=symbol
        )

    def cancel_all_orders(self, symbol: Optional[str] = None) -> dict[str, Any]:
        return self._execution_disabled_envelope(
            "cancel_all_orders", symbol=symbol
        )

    def execute(self, signal: NormalizedSignal) -> ExecutionResult:
        """Execute path used by the webhook handler.

        This method intentionally does not consult settings or
        ``submit_market_order`` — the webhook handler owns the
        provider-aware dispatch (so it can journal a dry-run preview
        without calling the network). It is kept here so the base
        ``BrokerBase.execute`` contract holds; calling it directly is a
        misuse and surfaces as a clear ``NotImplementedError``.
        """
        raise NotImplementedError(
            "topstep_execute_via_webhook_handler: Topstep order routing "
            "goes through the webhook handler's dispatch, not "
            "broker.execute(). Use submit_market_order() or the "
            "/api/topstep/* endpoints."
        )
