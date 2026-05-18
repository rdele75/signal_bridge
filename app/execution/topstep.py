"""Topstep / TopstepX broker adapter — PLACEHOLDER.

Topstep is the primary planned live broker target for SignalBridge, but
real order placement is not implemented in this build. The adapter is
instantiable so the app can boot with BROKER_PROVIDER=topstep, but any
attempt to actually `execute()` a signal raises NotImplementedError.

All read-only query methods return a structured "not implemented"
envelope (never raise) so the dashboard and /api/broker/* endpoints
stay safe.
"""
from __future__ import annotations

from typing import Any, Optional

from ..schemas import ExecutionResult, NormalizedSignal
from .broker_base import BrokerBase


def _has(value: str) -> bool:
    return bool(value and value.strip())


class TopstepBroker(BrokerBase):
    name = "topstep"
    provider = "topstep"
    # Marked "demo" rather than "live" so /status never advertises a live
    # path that doesn't exist.
    execution_mode = "demo"

    def __init__(
        self,
        *,
        username: str = "",
        password: str = "",
        api_key: str = "",
        account_id: str = "",
        env: str = "demo",
    ) -> None:
        self.username = username
        self.password = password
        self.api_key = api_key
        self.account_id = account_id
        self.env = env

    # ------------------------------------------------------------------
    # Read-only queries
    # ------------------------------------------------------------------

    def _credentials_summary(self) -> dict[str, Any]:
        return {
            "username_set": _has(self.username),
            "password_set": _has(self.password),
            "api_key_set": _has(self.api_key),
            "account_id_set": _has(self.account_id),
            "env": self.env or "demo",
        }

    def test_connection(self) -> dict[str, Any]:
        return {
            "ok": False,
            "provider": self.provider,
            "status": "not_implemented",
            "not_implemented": True,
            "message": (
                "topstep test_connection not implemented yet — "
                "Topstep / TopstepX adapter is a placeholder"
            ),
            "credentials": self._credentials_summary(),
        }

    def get_accounts(self) -> dict[str, Any]:
        return self._not_implemented(
            "get_accounts",
            accounts=[],
            credentials=self._credentials_summary(),
        )

    def get_selected_account(self) -> dict[str, Any]:
        return self._not_implemented(
            "get_selected_account",
            selected_account_id=self.account_id or None,
        )

    def get_positions(self) -> dict[str, Any]:
        return self._not_implemented("get_positions", positions=[])

    def get_orders(self) -> dict[str, Any]:
        return self._not_implemented("get_orders", orders=[])

    # ------------------------------------------------------------------
    # Mutating actions
    # ------------------------------------------------------------------

    def submit_market_order(self, signal: NormalizedSignal) -> dict[str, Any]:
        return self._not_implemented(
            "submit_market_order",
            symbol=signal.symbol,
            action=signal.action,
            contracts=signal.contracts,
        )

    def flatten_position(self, symbol: Optional[str] = None) -> dict[str, Any]:
        return self._not_implemented("flatten_position", symbol=symbol)

    def cancel_all_orders(self, symbol: Optional[str] = None) -> dict[str, Any]:
        return self._not_implemented("cancel_all_orders", symbol=symbol)

    # ------------------------------------------------------------------
    # Webhook execute path
    # ------------------------------------------------------------------

    def execute(self, signal: NormalizedSignal) -> ExecutionResult:
        # Webhook handler catches this and converts it into a clearly
        # labeled rejection rather than silently no-op'ing a real order.
        raise NotImplementedError(
            "topstep_adapter_not_implemented: Topstep / TopstepX live "
            "execution is a planned feature and not implemented yet. "
            "Use BROKER_PROVIDER=paper for now."
        )
