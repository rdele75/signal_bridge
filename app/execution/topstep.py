"""Topstep / TopstepX (ProjectX) broker adapter.

This adapter implements:

* ``authenticate()`` / ``get_auth_headers()`` / ``refresh_token()`` —
  ``/api/Auth/loginKey`` + JWT cache (23h TTL).
* ``get_accounts()`` / ``get_selected_account()`` —
  ``/api/Account/search`` with ``onlyActiveAccounts=true``.
* ``get_positions()`` — ``/api/Position/searchOpen``.
* ``get_orders()`` — ``/api/Order/searchOpen``.
* ``search_orders(startTimestamp, endTimestamp)`` —
  ``/api/Order/search``.
* ``submit_market_order()`` — builds a payload via
  ``topstep_order_builder.build_market_order_payload`` and submits to
  ``/api/Order/place`` when execution_mode is ``armed``; in ``test``
  mode the payload is built and journaled but not POSTed.
* ``flatten_position`` / ``cancel_all_orders`` — close existing state
  via ``/api/Position/closeContract`` / ``/api/Order/cancel``.

Execution states (post-collapse, 2026-05-21):

* ``off``    — webhook short-circuits before the adapter is touched.
* ``test``   — ``submit_market_order`` builds the payload, journals the
                attempt, and returns a ``submitted=false, mode=test``
                envelope without POSTing.
* ``armed``  — ``submit_market_order`` runs the armed gate stack and
                POSTs. Gates: credentials present, account numeric +
                ``canTrade`` (when known), kill switch off (when
                ``ENABLE_KILL_SWITCH`` is true), signal symbol in
                ``allowed_symbols``, contracts ≤
                ``max_contracts_per_trade``.

Confirmation tokens and the multi-step arming ceremony from the
pre-collapse model have been removed. The dashboard's mode dropdown
flips state atomically.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional, TYPE_CHECKING

import httpx

from ..schemas import ExecutionResult, NormalizedSignal
from .broker_base import BrokerBase
from .topstep_order_builder import build_market_order_payload

if TYPE_CHECKING:  # pragma: no cover - import cycle avoidance
    from ..journal import Journal


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


# ProjectX errorCode values that indicate the request failed because of
# authentication (token expired / missing / invalid), not a business
# rejection. Codes inferred from the existing "phantom errorCode=3"
# workaround in __init__ + ProjectX convention (1 = unauthorized).
# Submission paths use this set to decide whether to re-auth + retry
# once before giving up (H5).
AUTH_ERROR_CODES: frozenset[int] = frozenset({1, 3})


def _is_auth_failure(http_status: int, response: Any) -> bool:
    """True iff a ProjectX response looks like an auth rejection.

    Used by submit paths to decide whether to re-authenticate and
    retry once. Conservative — matches HTTP 401 OR a recognized
    errorCode in the response body. Any other status / errorCode is a
    real business rejection (or a transport error) and is NOT retried.
    """
    if http_status == 401:
        return True
    if not isinstance(response, dict):
        return False
    code = response.get("errorCode")
    try:
        return int(code) in AUTH_ERROR_CODES
    except (TypeError, ValueError):
        return False


class TopstepBroker(BrokerBase):
    name = "topstep"
    provider = "topstep"
    execution_mode = "off"

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
        # Post-collapse: a single execution_mode (off / test / armed)
        # plus the structural caps. The pre-collapse arming-token plumbing
        # is gone.
        execution_mode: str = "off",
        allowed_symbols: Optional[list[str]] = None,
        max_contracts_per_trade: int = 1,
        kill_switch_active: bool = False,
        kill_switch_enabled: bool = True,
        journal: Optional["Journal"] = None,
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
        # Per-account canTrade cache. Populated by ``get_accounts`` whenever
        # the call succeeds. Maps the trimmed-string account id to whatever
        # ProjectX reported for ``canTrade``. ``None`` (no entry) means we've
        # never received an account snapshot covering this id — the gate
        # falls open in that case (with a one-shot WARNING) so a fresh boot
        # doesn't refuse to submit before the operator has clicked Fetch
        # Accounts.
        self._can_trade_cache: dict[str, bool] = {}
        self._can_trade_warned: bool = False
        self.env = (env or "demo").lower()
        self.base_url = (base_url or DEFAULT_BASE_URL).strip().rstrip("/")
        self.ws_url = (ws_url or DEFAULT_WS_URL).strip().rstrip("/")
        self.token = (token or "").strip()
        self.token_expires_at = (token_expires_at or "").strip()
        self._token_sink = token_sink
        self._http_timeout = http_timeout
        self.execution_mode = (execution_mode or "off").lower()
        self.allowed_symbols = list(
            allowed_symbols if allowed_symbols is not None
            else ["MES1!", "MNQ1!", "NQ1!", "ES1!"]
        )
        self.max_contracts_per_trade = int(max_contracts_per_trade or 1)
        self.kill_switch_active = bool(kill_switch_active)
        self.kill_switch_enabled = bool(kill_switch_enabled)
        # Journal handle is optional so test harnesses can construct
        # the adapter without one. When set, ``submit_market_order``
        # spawns a daemon thread on each EXIT/COVER to look up the
        # fill via ``/api/Order/search`` and persist a closed_trade.
        self.journal: Optional["Journal"] = journal

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
            except Exception:  # pragma: no cover - best-effort persistence
                # L1 — log the full traceback, not just the class name.
                # Otherwise a silent persistence failure here means the
                # next restart forgets the token without an explanation.
                log.warning(
                    "topstep token persistence failed",
                    exc_info=True,
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
        # M1 — refresh the canTrade cache from the live snapshot so the
        # execution-safety check can consult it on the next submit.
        self._refresh_can_trade_cache(accounts)
        return {
            "ok": True,
            "provider": self.provider,
            "status": "ok",
            "message": f"{len(accounts)} active account(s)",
            "accounts": accounts,
            "credentials": self._credentials_summary(),
        }

    def _refresh_can_trade_cache(
        self, accounts: list[dict[str, Any]]
    ) -> None:
        """Update the canTrade cache from a fresh ``get_accounts`` payload.

        Only accounts whose normalized payload carries an explicit
        ``can_trade`` boolean overwrite the cache; anything else leaves
        the prior entry alone (so a partial refresh doesn't drop a
        previously-known account's flag)."""
        for acct in accounts:
            raw_id = acct.get("id")
            if raw_id is None:
                continue
            can_trade = acct.get("can_trade")
            if isinstance(can_trade, bool):
                self._can_trade_cache[str(raw_id).strip()] = can_trade

    def _account_can_trade(self) -> Optional[bool]:
        """Return the cached canTrade flag for the currently selected
        account. ``None`` means we've never received a snapshot — the
        execution-safety gate silently bypasses the check in that case
        (matching the "if known" language in the public docs)."""
        target = (self.account_id or "").strip()
        if not target:
            return None
        return self._can_trade_cache.get(target)

    def _warn_can_trade_unknown_once(self) -> None:
        """Log the canTrade-unknown WARNING at most once per process
        lifetime so the audit trail records the "if known" bypass
        without spamming every signal."""
        if self._can_trade_warned:
            return
        self._can_trade_warned = True
        log.warning(
            "Topstep canTrade gate is unenforced for account %s — no "
            "accounts snapshot is cached. Fetch Accounts on the broker "
            "page to populate the cache so the gate can act. Subsequent "
            "signals will not repeat this warning.",
            self.account_id or "(unset)",
        )

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

    @staticmethod
    def _normalize_order_row(raw: dict[str, Any]) -> dict[str, Any]:
        """Project a ProjectX order row into the dashboard-friendly shape.

        Unknown fields come back as ``None`` rather than fabricated. The
        side mapping mirrors the order builder: 0=buy/long, 1=sell/short.
        """
        side_value = raw.get("side")
        side_label = None
        if side_value == 0:
            side_label = "BUY"
        elif side_value == 1:
            side_label = "SELL"
        elif side_value is not None:
            side_label = str(side_value)

        size = raw.get("size")
        if size is None:
            size = raw.get("filledSize")

        return {
            "orderId": str(raw["id"]) if raw.get("id") is not None else None,
            "accountId": raw.get("accountId"),
            "contractId": raw.get("contractId"),
            "creationTimestamp": (
                raw.get("creationTimestamp") or raw.get("createTimestamp")
            ),
            "updateTimestamp": raw.get("updateTimestamp"),
            "status": raw.get("status"),
            "type": raw.get("type"),
            "side": side_value,
            "side_label": side_label,
            "size": size,
            "limitPrice": raw.get("limitPrice"),
            "stopPrice": raw.get("stopPrice"),
            "filledPrice": (
                raw.get("filledPrice") or raw.get("averageFilledPrice")
            ),
            "customTag": raw.get("customTag"),
        }

    def get_order_history(
        self,
        *,
        lookback_days: Optional[int] = None,
        limit: Optional[int] = None,
        start_timestamp: Optional[str] = None,
        end_timestamp: Optional[str] = None,
    ) -> dict[str, Any]:
        """Return recent Topstep orders, normalized for the dashboard.

        Defaults: ``lookback_days=7``, ``limit=100``. Explicit
        ``start_timestamp`` / ``end_timestamp`` always win over the
        derived window. Returns a structured envelope — never raises so
        the JSON endpoint stays stable on adapter error.
        """
        if lookback_days is not None:
            lookback_days = max(1, int(lookback_days))
        if limit is not None:
            limit = max(1, int(limit))
        if not start_timestamp and lookback_days:
            start_timestamp = (
                datetime.now(timezone.utc)
                - timedelta(days=lookback_days)
            ).isoformat()
        if not end_timestamp:
            end_timestamp = datetime.now(timezone.utc).isoformat()

        result = self.search_orders(
            start_timestamp=start_timestamp,
            end_timestamp=end_timestamp,
        )
        raw_orders = result.get("orders") or []
        if not isinstance(raw_orders, list):
            raw_orders = []
        normalized = [
            self._normalize_order_row(row)
            for row in raw_orders
            if isinstance(row, dict)
        ]
        if limit is not None:
            normalized = normalized[:limit]
        envelope = {
            "ok": bool(result.get("ok")),
            "provider": self.provider,
            "status": result.get("status", "unknown"),
            "http_status": result.get("http_status"),
            "message": result.get("message", ""),
            "selected_account_id": self.account_id or None,
            "lookback_days": lookback_days,
            "limit": limit,
            "start_timestamp": start_timestamp,
            "end_timestamp": end_timestamp,
            "orders": normalized,
            "count": len(normalized),
        }
        # Surface credential-presence info but never the actual key/token.
        envelope["credentials"] = result.get("credentials") or {}
        for forbidden in ("error_code", "error_message"):
            if forbidden in result:
                envelope[forbidden] = result[forbidden]
        return envelope

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
            "status": "topstep_execution_not_armed",
            "not_implemented": True,
            "message": (
                "Topstep order submission refused: execution is not armed."
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
            "allowed_symbols": list(self.allowed_symbols),
            "max_contracts_per_trade": self.max_contracts_per_trade,
            "kill_switch_active": self.kill_switch_active,
            "kill_switch_enabled": self.kill_switch_enabled,
            "selected_account_id": self.account_id or None,
        }

    def _armed_safety_check(
        self,
        signal: Optional[NormalizedSignal] = None,
        *,
        bypass_kill_switch: bool = False,
    ) -> Optional[str]:
        """Return ``None`` when every armed-mode gate is satisfied, else
        the identifier of the first failing gate.

        Called from ``submit_market_order`` when ``execution_mode ==
        "armed"`` and from ``flatten_position`` / ``cancel_all_orders``
        (which bypass the kill-switch gate — closing existing state
        must remain available after emergency stop).

        ``signal`` is optional: when supplied, the symbol allowlist and
        contract cap are enforced; otherwise they're skipped (used by
        panel rendering / verify endpoints).
        """
        if self.execution_mode != "armed":
            return "execution_not_armed"
        if not self._has_required_credentials():
            return "missing_credentials"
        if self._numeric_account_id() is None:
            return "non_numeric_account_id"
        can_trade = self._account_can_trade()
        if can_trade is False:
            return "account_cannot_trade"
        if can_trade is None:
            self._warn_can_trade_unknown_once()
        if (
            not bypass_kill_switch
            and self.kill_switch_enabled
            and self.kill_switch_active
        ):
            return "kill_switch_active"
        if signal is not None:
            allowed = [
                s.strip() for s in self.allowed_symbols
                if s and s.strip()
            ]
            if not allowed:
                return "symbol_not_allowed"
            if signal.symbol not in allowed:
                return "symbol_not_allowed"
            cap = max(int(self.max_contracts_per_trade or 0), 0)
            if cap <= 0:
                return "contracts_above_max"
            if signal.contracts > cap:
                return "contracts_above_max"
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
        """Build (and conditionally submit) a market order.

        Behavior by execution_mode:

        * ``off``    — caller should not reach this method. If called,
                      returns a refusal envelope with status
                      ``execution_not_armed``.
        * ``test``   — builds the ``/api/Order/place`` payload, logs it,
                      returns ``{ok: True, submitted: False, mode: "test",
                      payload, ...}``. Never touches the network.
        * ``armed``  — runs the armed gate stack; on pass, POSTs to
                      ``/api/Order/place`` and returns the parsed
                      response.

        Auth-failure retry (H5): for armed submissions the local 23h
        token-validity check can disagree with the server. If the first
        POST returns HTTP 401 or a ProjectX ``errorCode`` matching the
        documented auth-rejection codes, this method calls
        ``authenticate()`` once and retries the POST exactly once.
        Non-auth failures are NOT retried.
        """
        # Test mode short-circuits BEFORE the armed gate stack — Test
        # is for plumbing verification, so it intentionally tolerates
        # an active kill switch and missing canTrade. The general risk
        # engine has already vetted the signal at this point (symbol
        # in ALLOWED_SYMBOLS, contracts ≤ MAX_CONTRACTS_PER_TRADE,
        # direction toggles, daily loss, open positions).
        if self.execution_mode == "test":
            built = self.build_order_preview(signal, symbol_map=symbol_map)
            if not built.get("ok"):
                envelope = dict(built)
                envelope.update(
                    {
                        "ok": False,
                        "accepted": False,
                        "status": built.get("reason", "order_build_failed"),
                        "provider": self.provider,
                        "submitted": False,
                        "mode": "test",
                        "would_submit": False,
                        "safety": self._safety_state(),
                    }
                )
                envelope.setdefault(
                    "message",
                    f"Test order build failed: {built.get('reason')}",
                )
                return envelope
            log.info(
                "topstep test-mode order built (no POST): symbol=%s "
                "action=%s contracts=%s account=%s",
                signal.symbol,
                signal.action,
                signal.contracts,
                self.account_id or "(none)",
            )
            return {
                "ok": True,
                "accepted": True,
                "status": "test_built",
                "provider": self.provider,
                "submitted": False,
                "mode": "test",
                "would_submit": True,
                "message": (
                    "Test-mode order built and validated; not submitted "
                    "to ProjectX."
                ),
                "payload": built["payload"],
                "account_id": built.get("account_id"),
                "contract_id": built.get("contract_id"),
                "side": built.get("side"),
                "size": built.get("size"),
                "safety": self._safety_state(),
            }

        safety_gate = self._armed_safety_check(signal)
        if safety_gate is not None:
            envelope = self._execution_disabled_envelope(
                "submit_market_order",
                accepted=False,
                status=safety_gate,
                gate=safety_gate,
                mode=self.execution_mode,
                submitted=False,
                symbol=signal.symbol,
                broker_symbol=signal.broker_symbol,
                action=signal.action,
                contracts=signal.contracts,
                safety=self._safety_state(),
            )
            envelope["message"] = (
                f"Topstep armed-mode order refused: {safety_gate}"
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
        if _is_auth_failure(http_status, response):
            log.info(
                "topstep order place auth failure (http=%s errorCode=%s) — "
                "re-authenticating and retrying once",
                http_status,
                response.get("errorCode") if isinstance(response, dict) else None,
            )
            auth_retry = self.authenticate()
            if auth_retry.get("ok"):
                http_status, response = self._post_json(
                    "/api/Order/place", payload, auth=True
                )
            # If the re-auth itself failed, fall through with the
            # original (auth-failed) response — the existing rejection
            # envelope already conveys what went wrong. We do NOT loop.

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
            # Spawn a daemon thread to look up the fill from
            # /api/Order/search and persist a closed_trades row when
            # the submitted order was an EXIT/COVER. The thread does
            # NOT block the webhook response — submission acks in the
            # foreground; reconciliation runs in the background.
            self._spawn_fill_reconcile_thread(signal, str(order_id))
            return {
                "ok": True,
                "accepted": True,
                "status": "submitted",
                "provider": self.provider,
                "submitted": True,
                "mode": "armed",
                "http_status": http_status,
                "broker_order_id": str(order_id),
                "order_id": str(order_id),
                "message": "topstep armed order submitted",
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

    # ------------------------------------------------------------------
    # Reactive close-trade reconciliation
    #
    # Topstep doesn't fill orders synchronously in the /api/Order/place
    # response — the placed order acks with an orderId, then the fill
    # surfaces on /api/Order/search 1-2s later. To populate the
    # dashboard's P&L card the moment a close trade settles, every
    # armed EXIT/COVER submission spawns a daemon thread that polls
    # the order back and pairs it FIFO against the oldest unmatched
    # entry signal for the symbol. The periodic poll (D3) backstops
    # this path so transient API failures don't permanently lose a
    # close.
    # ------------------------------------------------------------------

    # Actions whose successful fill closes an existing position. SELL
    # is excluded here — the SignalBridge risk engine treats SELL as a
    # short-entry, not a long-exit, so a SELL submission opens a short
    # rather than closing a long. EXIT and COVER are the unambiguous
    # close-side actions.
    _CLOSING_ACTIONS: frozenset[str] = frozenset({"EXIT", "COVER"})

    def _spawn_fill_reconcile_thread(
        self, signal: NormalizedSignal, broker_order_id: str
    ) -> Optional[threading.Thread]:
        """Spawn the daemon that reconciles a Topstep fill into the
        journal's ``closed_trades`` table. Returns the started thread
        for tests; in production the result is ignored.

        Skips the spawn (and logs at DEBUG) when:
        * the signal isn't a close-side action,
        * no journal was wired into the adapter,
        * the broker order id is empty.
        """
        if self.journal is None:
            log.debug(
                "reconcile skip: no journal handle (order_id=%s)",
                broker_order_id,
            )
            return None
        if not broker_order_id:
            return None
        action = (signal.action or "").upper()
        if action not in self._CLOSING_ACTIONS:
            return None
        thread = threading.Thread(
            target=self._reconcile_fill_after_submit,
            args=(signal, broker_order_id),
            daemon=True,
            name=f"signalbridge-reconcile-{broker_order_id}",
        )
        thread.start()
        return thread

    def _reconcile_fill_after_submit(
        self, signal: NormalizedSignal, broker_order_id: str
    ) -> None:
        """Background body of the reactive reconciliation thread.

        Sleeps briefly to let Topstep reflect the fill, fetches recent
        orders, pairs the fill with an open entry, and records a
        ``closed_trades`` row. Never raises — exceptions are logged so
        a daemon thread can't crash the process.
        """
        try:
            self._do_reconcile(signal, broker_order_id, attempt=1)
        except Exception:  # pragma: no cover - daemon thread guard
            log.warning(
                "signalbridge closed_trade reconciliation crashed for "
                "order_id=%s",
                broker_order_id,
                exc_info=True,
            )

    def _do_reconcile(
        self,
        signal: NormalizedSignal,
        broker_order_id: str,
        *,
        attempt: int,
    ) -> None:
        """Single reconciliation attempt. Sleeps, fetches, pairs, writes.

        ``attempt`` is 1 on the initial run and 2 on the retry. The
        retry uses a longer sleep to give Topstep more time to reflect
        a slow fill. After attempt 2 with no fill, the periodic poll
        will pick it up later.
        """
        sleep_seconds = 2.0 if attempt == 1 else 3.0
        time.sleep(sleep_seconds)
        if self.journal is None:  # pragma: no cover - guarded above
            return
        if self.journal.closed_trade_exists_for_order_id(broker_order_id):
            return
        fill = self._lookup_recent_fill(broker_order_id)
        if fill is None:
            if attempt < 2:
                self._do_reconcile(signal, broker_order_id, attempt=attempt + 1)
                return
            log.warning(
                "signalbridge closed_trade fill_not_found order_id=%s "
                "symbol=%s — periodic poll will retry",
                broker_order_id,
                signal.symbol,
            )
            return
        self._record_reconciled_close(
            signal=signal,
            broker_order_id=broker_order_id,
            fill=fill,
            source="reactive",
        )

    def _lookup_recent_fill(
        self, broker_order_id: str
    ) -> Optional[dict[str, Any]]:
        """Return the normalized order row matching ``broker_order_id``
        from a ``/api/Order/search`` window covering the last 2 minutes,
        or ``None`` on miss/error. Errors are logged at WARNING — the
        periodic poll will retry. ``filledPrice`` may still be missing
        when Topstep hasn't reflected the fill yet; callers should
        treat a row without a fill price as a miss too.
        """
        start_ts = (
            datetime.now(timezone.utc) - timedelta(minutes=2)
        ).isoformat()
        end_ts = datetime.now(timezone.utc).isoformat()
        try:
            history = self.get_order_history(
                start_timestamp=start_ts, end_timestamp=end_ts
            )
        except Exception:  # pragma: no cover - defensive
            log.warning(
                "signalbridge closed_trade get_order_history raised "
                "order_id=%s",
                broker_order_id,
                exc_info=True,
            )
            return None
        if not history.get("ok"):
            log.warning(
                "signalbridge closed_trade search_orders not_ok "
                "order_id=%s status=%s",
                broker_order_id,
                history.get("status"),
            )
            return None
        for row in history.get("orders") or []:
            if not isinstance(row, dict):
                continue
            if str(row.get("orderId") or "") != broker_order_id:
                continue
            fill_price = row.get("filledPrice")
            if fill_price is None:
                continue
            return row
        return None

    def _record_reconciled_close(
        self,
        *,
        signal: NormalizedSignal,
        broker_order_id: str,
        fill: dict[str, Any],
        source: str,
    ) -> None:
        """Pair ``fill`` with an open entry signal and persist a
        ``closed_trades`` row. ``source`` is just a log token so the
        operator can ``grep`` reactive vs. periodic activity.
        """
        from ..risk_engine import INSTRUMENT_POINT_VALUES_USD

        if self.journal is None:  # pragma: no cover - guarded
            return
        symbol = signal.symbol
        entry = self.journal.find_open_entry_for_symbol(symbol)
        if entry is None:
            log.warning(
                "signalbridge closed_trade no_open_entry symbol=%s "
                "order_id=%s source=%s — fill is unmatched",
                symbol,
                broker_order_id,
                source,
            )
            return
        try:
            exit_price = float(fill.get("filledPrice"))
        except (TypeError, ValueError):
            log.warning(
                "signalbridge closed_trade bad_fill_price order_id=%s "
                "value=%r",
                broker_order_id,
                fill.get("filledPrice"),
            )
            return
        try:
            entry_price = (
                float(entry["price"]) if entry.get("price") is not None else None
            )
        except (TypeError, ValueError):
            entry_price = None
        if entry_price is None:
            log.warning(
                "signalbridge closed_trade missing_entry_price order_id=%s "
                "entry_signal_id=%s — recording with NULL entry_price",
                broker_order_id,
                entry.get("id"),
            )
        # FIFO sizing: a partial-close shrinks the matched entry's
        # contract count but the simple model records ONE row per fill
        # at the smaller of the entry's size and the fill's size. The
        # periodic poll catches anything the simple path misses (e.g.
        # multi-leg partials).
        entry_contracts = int(entry.get("contracts") or 0)
        fill_size = int(fill.get("size") or 0)
        if entry_contracts <= 0 or fill_size <= 0:
            contracts_closed = max(entry_contracts, fill_size, 1)
        else:
            contracts_closed = min(entry_contracts, fill_size)
        entry_action = (entry.get("action") or "").upper()
        is_long_entry = entry_action in {"BUY", "LONG"}
        side_label = "long" if is_long_entry else "short"
        direction = 1.0 if is_long_entry else -1.0
        if entry_price is None:
            pnl_points = 0.0
        else:
            pnl_points = (exit_price - entry_price) * direction
        multiplier = INSTRUMENT_POINT_VALUES_USD.get(symbol)
        pnl_dollars: Optional[float]
        if multiplier is None:
            log.warning(
                "signalbridge closed_trade no_multiplier symbol=%s "
                "order_id=%s — pnl_dollars=NULL (won't contribute to the "
                "daily-loss cap or dashboard $ P&L)",
                symbol,
                broker_order_id,
            )
            pnl_dollars = None
        else:
            pnl_dollars = pnl_points * multiplier * contracts_closed
        self.journal.record_closed_trade(
            symbol=symbol,
            side=side_label,
            contracts=contracts_closed,
            entry_price=entry_price,
            exit_price=exit_price,
            realized_pnl_points=pnl_points,
            realized_pnl_dollars=pnl_dollars,
            broker_provider=self.provider,
            topstep_order_id=broker_order_id,
        )
        log.info(
            "signalbridge closed_trade source=%s symbol=%s side=%s "
            "contracts=%d entry=%s exit=%.4f pnl_points=%.4f "
            "pnl_dollars=%s order_id=%s",
            source,
            symbol,
            side_label,
            contracts_closed,
            f"{entry_price:.4f}" if entry_price is not None else "n/a",
            exit_price,
            pnl_points,
            f"{pnl_dollars:.2f}" if pnl_dollars is not None else "n/a",
            broker_order_id,
        )

    # ------------------------------------------------------------------
    # Exit helpers (flatten / cancel)
    #
    # Kill switch blocks NEW entries; these close EXISTING state. The
    # safety gates still apply (auth, account ack, canTrade, etc.) but
    # the kill-switch check is bypassed so an operator can still exit
    # after hitting emergency stop.
    # ------------------------------------------------------------------

    def _flatten_not_armed_noop(
        self, op: str, symbol: Optional[str]
    ) -> dict[str, Any]:
        """Envelope returned when flatten/cancel is invoked while
        execution is not Armed. We never fake the action with phantom
        orders — positions live on Topstep's side and must be closed
        through a real ``/api/Position/closeContract`` call, which only
        runs in Armed mode."""
        return {
            "ok": False,
            "provider": self.provider,
            "status": "not_armed",
            "message": (
                f"{op} requires execution_mode=armed — close positions "
                "from the dashboard's Armed state or directly in TopstepX"
            ),
            "symbol": symbol,
            "legs": [],
            "positions_before": 0,
            "safety": self._safety_state(),
        }

    def _exit_safety_refusal(
        self, op: str, gate: str, symbol: Optional[str], container_key: str
    ) -> dict[str, Any]:
        """Build a structured refusal for flatten/cancel when a safety
        gate is open. Mirrors ``submit_market_order``'s refusal shape."""
        return {
            "ok": False,
            "provider": self.provider,
            "status": gate,
            "gate": gate,
            "message": f"Topstep {op} refused: {gate}",
            "symbol": symbol,
            container_key: [],
            "safety": self._safety_state(),
        }

    def _post_close_with_auth_retry(
        self, path: str, payload: dict[str, Any]
    ) -> tuple[int, Any]:
        """POST ``path`` with bearer auth, retrying once on an auth
        failure. Mirrors the H5 retry pattern from
        ``submit_market_order`` so a stale-but-locally-valid token
        gets one shot at re-auth before the leg is recorded as failed.
        """
        http_status, response = self._post_json(path, payload, auth=True)
        if _is_auth_failure(http_status, response):
            log.info(
                "topstep %s auth failure (http=%s errorCode=%s) — "
                "re-authenticating and retrying once",
                path,
                http_status,
                response.get("errorCode") if isinstance(response, dict) else None,
            )
            auth_retry = self.authenticate()
            if auth_retry.get("ok"):
                http_status, response = self._post_json(
                    path, payload, auth=True
                )
        return http_status, response

    @staticmethod
    def _position_matches_symbol(
        position: dict[str, Any], symbol: str
    ) -> bool:
        """Decide whether ``position`` falls under the operator-supplied
        symbol filter. We accept three matches in this order:

        1. exact equality with the contract id (ProjectX's identifier);
        2. exact equality with any plausible label field (``symbol``,
           ``ticker``);
        3. substring match against the contract id (so ``"MES"`` matches
           ``"CON.F.US.MES.M26"``).

        Substring match is forgiving on purpose — the operator types
        the TV ticker, the position carries a broker contract id, and
        we don't have a reverse mapping at this layer.
        """
        needle = (symbol or "").strip()
        if not needle:
            return True
        for key in ("contractId", "symbol", "ticker"):
            value = position.get(key)
            if value is None:
                continue
            if str(value) == needle:
                return True
        contract_id = position.get("contractId")
        if contract_id and needle in str(contract_id):
            return True
        return False

    @staticmethod
    def _closing_side_for_position(position: dict[str, Any]) -> Optional[str]:
        """Return the side a closing market order would use, given the
        ProjectX position row. Long (type 1) closes with SELL; short
        (type 2) closes with BUY. Unknown shapes return ``None`` so
        the leg leaves ``side`` blank rather than misreporting.
        """
        pos_type = position.get("type")
        try:
            pos_type_int = int(pos_type) if pos_type is not None else None
        except (TypeError, ValueError):
            return None
        if pos_type_int == 1:
            return "SELL"
        if pos_type_int == 2:
            return "BUY"
        return None

    def _flatten_one_position(
        self, position: dict[str, Any], numeric_account_id: int
    ) -> dict[str, Any]:
        """Close one ProjectX position via ``/api/Position/closeContract``.

        Returns the structured leg result for the flatten envelope.
        Never raises — transport / auth / business failures are
        captured in ``status`` + ``message``.

        ``closeContract`` is preferred over an opposite-side market
        order because it is atomic and the broker handles direction.
        Documented ProjectX request shape:
            POST /api/Position/closeContract
            {"accountId": <int>, "contractId": "<str>"}
        """
        contract_id = position.get("contractId")
        size_raw = position.get("size")
        try:
            size = int(size_raw) if size_raw is not None else 0
        except (TypeError, ValueError):
            size = 0
        side = self._closing_side_for_position(position)

        leg: dict[str, Any] = {
            "symbol": (
                position.get("symbol") or position.get("ticker") or contract_id
            ),
            "contract_id": contract_id,
            "size": size,
            "side": side,
            "ok": False,
            "order_id": None,
        }

        if not contract_id:
            leg.update(
                status="invalid_position",
                message="position row missing contractId — cannot close",
            )
            return leg

        http_status, response = self._post_close_with_auth_retry(
            "/api/Position/closeContract",
            {"accountId": numeric_account_id, "contractId": contract_id},
        )
        leg["http_status"] = http_status

        if http_status == 0:
            leg.update(
                status="network_error",
                message=(
                    response
                    if isinstance(response, str)
                    else "topstep closeContract network error"
                ),
            )
            return leg

        if not isinstance(response, dict):
            leg.update(
                status="close_failed",
                message=(
                    f"topstep closeContract returned non-JSON ({http_status})"
                ),
            )
            return leg

        success_flag = bool(response.get("success"))
        error_code = response.get("errorCode")
        error_message = response.get("errorMessage")
        order_id = response.get("orderId")

        if http_status == 200 and success_flag:
            leg.update(
                ok=True,
                status="accepted",
                message="position closed",
                order_id=str(order_id) if order_id is not None else None,
                error_code=error_code,
            )
            return leg

        leg.update(
            status="close_rejected",
            error_code=error_code,
            error_message=error_message,
            message=(
                str(error_message)
                if error_message
                else f"topstep closeContract rejected (errorCode={error_code})"
            ),
        )
        return leg

    def _cancel_one_order(
        self, order: dict[str, Any], numeric_account_id: int
    ) -> dict[str, Any]:
        """Cancel one ProjectX working order via ``/api/Order/cancel``.

        Same one-shot auth retry as flatten. Returns the structured
        leg result; never raises.
        """
        order_id_raw = order.get("id")
        if order_id_raw is None:
            order_id_raw = order.get("orderId")
        try:
            order_id_int = (
                int(order_id_raw) if order_id_raw is not None else None
            )
        except (TypeError, ValueError):
            order_id_int = None

        leg: dict[str, Any] = {
            "symbol": (
                order.get("symbol")
                or order.get("ticker")
                or order.get("contractId")
            ),
            "contract_id": order.get("contractId"),
            "order_id": (
                str(order_id_raw) if order_id_raw is not None else None
            ),
            "ok": False,
        }

        if order_id_int is None:
            leg.update(
                status="invalid_order",
                message="order row missing numeric id — cannot cancel",
            )
            return leg

        http_status, response = self._post_close_with_auth_retry(
            "/api/Order/cancel",
            {"accountId": numeric_account_id, "orderId": order_id_int},
        )
        leg["http_status"] = http_status

        if http_status == 0:
            leg.update(
                status="network_error",
                message=(
                    response
                    if isinstance(response, str)
                    else "topstep cancel network error"
                ),
            )
            return leg

        if not isinstance(response, dict):
            leg.update(
                status="cancel_failed",
                message=f"topstep cancel returned non-JSON ({http_status})",
            )
            return leg

        success_flag = bool(response.get("success"))
        error_code = response.get("errorCode")
        error_message = response.get("errorMessage")

        if http_status == 200 and success_flag:
            leg.update(
                ok=True,
                status="cancelled",
                message="order cancelled",
                error_code=error_code,
            )
            return leg

        leg.update(
            status="cancel_rejected",
            error_code=error_code,
            error_message=error_message,
            message=(
                str(error_message)
                if error_message
                else f"topstep cancel rejected (errorCode={error_code})"
            ),
        )
        return leg

    @staticmethod
    def _summarize_legs(legs: list[dict[str, Any]], success_label: str) -> tuple[bool, str]:
        """Collapse a leg list into ``(ok, status)`` for the top-level
        envelope. ``success_label`` is the all-good status name
        (``flattened`` or ``cancelled``).
        """
        if not legs:
            return True, success_label
        ok_count = sum(1 for leg in legs if leg.get("ok"))
        if ok_count == len(legs):
            return True, success_label
        if ok_count == 0:
            return False, "failed"
        return False, "partial"

    def flatten_position(self, symbol: Optional[str] = None) -> dict[str, Any]:
        """Close one or every open Topstep position via
        ``/api/Position/closeContract``.

        Behavior:

        * Off / Test states → no-op envelope with ``status="not_armed"``.
          No phantom orders — positions live on Topstep's side and must
          be closed through a real broker call, which only happens in
          Armed.
        * Armed → runs the armed safety gates with the kill switch
          bypassed, fetches open positions, closes each one
          independently, and returns a structured envelope with one
          entry per leg.
        * ``symbol`` is optional. When set, only positions whose
          ``contractId`` matches are closed (exact or substring — see
          ``_position_matches_symbol``).

        Partial failures: legs are independent. If leg N fails, legs
        N+1..end are still attempted. The envelope reports every
        attempt so the operator can decide what to do next.
        """
        if self.execution_mode != "armed":
            return self._flatten_not_armed_noop("flatten", symbol)

        gate = self._armed_safety_check(bypass_kill_switch=True)
        if gate is not None:
            return self._exit_safety_refusal(
                "flatten", gate, symbol, container_key="legs"
            )

        positions_resp = self.get_positions()
        if not positions_resp.get("ok"):
            payload = dict(positions_resp)
            payload.setdefault("provider", self.provider)
            payload["legs"] = []
            payload["positions_before"] = 0
            payload["safety"] = self._safety_state()
            payload["message"] = (
                f"flatten aborted before any orders sent — "
                f"{payload.get('message', 'positions fetch failed')}"
            )
            return payload

        positions = positions_resp.get("positions") or []
        all_positions_count = len(positions)
        if symbol:
            positions = [
                p
                for p in positions
                if isinstance(p, dict)
                and self._position_matches_symbol(p, symbol)
            ]

        if not positions:
            status = "no_open_positions"
            if symbol and all_positions_count > 0:
                message = (
                    f"no open positions match {symbol!r} — "
                    f"{all_positions_count} other position(s) untouched"
                )
            else:
                message = "no open positions to flatten"
            return {
                "ok": True,
                "provider": self.provider,
                "status": status,
                "message": message,
                "symbol": symbol,
                "legs": [],
                "positions_before": all_positions_count,
                "safety": self._safety_state(),
            }

        # ``get_positions`` already validated numeric account id.
        numeric_account_id = self._numeric_account_id()
        # numeric_account_id is guaranteed non-None here because the
        # get_positions call would have failed first otherwise.
        assert numeric_account_id is not None

        legs: list[dict[str, Any]] = []
        for pos in positions:
            if not isinstance(pos, dict):
                legs.append(
                    {
                        "symbol": None,
                        "contract_id": None,
                        "size": 0,
                        "side": None,
                        "ok": False,
                        "order_id": None,
                        "status": "invalid_position",
                        "message": "position row was not a JSON object",
                    }
                )
                continue
            legs.append(self._flatten_one_position(pos, numeric_account_id))

        ok, status = self._summarize_legs(legs, "flattened")
        ok_count = sum(1 for leg in legs if leg.get("ok"))
        if status == "flattened":
            message = f"flattened {ok_count} of {len(legs)} position(s)"
        elif status == "partial":
            message = (
                f"flattened {ok_count} of {len(legs)} — "
                f"{len(legs) - ok_count} failed"
            )
        else:
            message = f"flatten failed for all {len(legs)} position(s)"

        return {
            "ok": ok,
            "provider": self.provider,
            "status": status,
            "message": message,
            "symbol": symbol,
            "legs": legs,
            "positions_before": all_positions_count,
            "safety": self._safety_state(),
        }

    def cancel_all_orders(self, symbol: Optional[str] = None) -> dict[str, Any]:
        """Cancel every open Topstep working order via
        ``/api/Order/cancel``.

        Same shape as ``flatten_position``: Off/Test states no-op,
        Armed runs the gates (kill switch bypassed), fetches working
        orders, cancels each independently, returns a per-leg envelope.
        Partial failures are reported, not raised.
        """
        if self.execution_mode != "armed":
            return {
                "ok": False,
                "provider": self.provider,
                "status": "not_armed",
                "message": (
                    "cancel-all requires execution_mode=armed — manage "
                    "non-armed orders in TopstepX directly"
                ),
                "symbol": symbol,
                "legs": [],
                "orders_before": 0,
                "safety": self._safety_state(),
            }

        gate = self._armed_safety_check(bypass_kill_switch=True)
        if gate is not None:
            return self._exit_safety_refusal(
                "cancel-all", gate, symbol, container_key="legs"
            )

        orders_resp = self.get_orders()
        if not orders_resp.get("ok"):
            payload = dict(orders_resp)
            payload.setdefault("provider", self.provider)
            payload["legs"] = []
            payload["orders_before"] = 0
            payload["safety"] = self._safety_state()
            payload["message"] = (
                f"cancel-all aborted before any cancels sent — "
                f"{payload.get('message', 'orders fetch failed')}"
            )
            return payload

        orders = orders_resp.get("orders") or []
        all_orders_count = len(orders)
        if symbol:
            orders = [
                o
                for o in orders
                if isinstance(o, dict)
                and self._position_matches_symbol(o, symbol)
            ]

        if not orders:
            if symbol and all_orders_count > 0:
                message = (
                    f"no open orders match {symbol!r} — "
                    f"{all_orders_count} other order(s) untouched"
                )
            else:
                message = "no open orders to cancel"
            return {
                "ok": True,
                "provider": self.provider,
                "status": "no_open_orders",
                "message": message,
                "symbol": symbol,
                "legs": [],
                "orders_before": all_orders_count,
                "safety": self._safety_state(),
            }

        numeric_account_id = self._numeric_account_id()
        assert numeric_account_id is not None

        legs: list[dict[str, Any]] = []
        for order in orders:
            if not isinstance(order, dict):
                legs.append(
                    {
                        "symbol": None,
                        "contract_id": None,
                        "order_id": None,
                        "ok": False,
                        "status": "invalid_order",
                        "message": "order row was not a JSON object",
                    }
                )
                continue
            legs.append(
                self._cancel_one_order(order, numeric_account_id)
            )

        ok, status = self._summarize_legs(legs, "cancelled")
        ok_count = sum(1 for leg in legs if leg.get("ok"))
        if status == "cancelled":
            message = f"cancelled {ok_count} of {len(legs)} order(s)"
        elif status == "partial":
            message = (
                f"cancelled {ok_count} of {len(legs)} — "
                f"{len(legs) - ok_count} failed"
            )
        else:
            message = f"cancel failed for all {len(legs)} order(s)"

        return {
            "ok": ok,
            "provider": self.provider,
            "status": status,
            "message": message,
            "symbol": symbol,
            "legs": legs,
            "orders_before": all_orders_count,
            "safety": self._safety_state(),
        }

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
