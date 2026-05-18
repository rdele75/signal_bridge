"""Abstract broker adapter interface.

Every adapter implements a uniform surface so the dashboard, API
endpoints, and webhook handler don't need to know which provider is
loaded. Methods that have not been built out for a given provider must
return a structured ``{"ok": False, "status": "not_implemented", ...}``
response — never raise — so the UI can safely render them.

The one exception is ``execute()``: a placeholder adapter raises
``NotImplementedError`` from ``execute()`` so the webhook handler can
turn that into a clearly labeled rejection rather than silently no-op a
real order.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Optional

from ..schemas import ExecutionResult, NormalizedSignal


class BrokerBase(ABC):
    """All broker adapters implement this interface.

    `provider` is the public name shown in /status and recorded with every
    journaled signal so a row's broker target is unambiguous.
    """

    name: str = "base"
    provider: str = "base"
    execution_mode: str = "paper"

    @abstractmethod
    def execute(self, signal: NormalizedSignal) -> ExecutionResult:
        """Execute a normalized signal and return an ExecutionResult."""
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Connection / status
    # ------------------------------------------------------------------

    def test_connection(self) -> dict[str, Any]:
        """Readiness probe for the dashboard "Test connection" button.

        Default returns "not implemented" so adapters that don't override
        this can't accidentally claim they work.
        """
        return self._not_implemented("test_connection")

    # ------------------------------------------------------------------
    # Account / position / order queries
    #
    # All of these MUST return JSON-friendly dicts and MUST NOT raise.
    # The dashboard and /api/broker/* endpoints call them blindly; an
    # adapter that hasn't implemented one should return the structured
    # not-implemented envelope below.
    # ------------------------------------------------------------------

    def get_accounts(self) -> dict[str, Any]:
        """List accounts visible to this adapter."""
        return self._not_implemented("get_accounts", accounts=[])

    def get_selected_account(self) -> dict[str, Any]:
        """Return the currently selected account id, if any."""
        return self._not_implemented(
            "get_selected_account", selected_account_id=None
        )

    def get_positions(self) -> dict[str, Any]:
        """List currently open positions."""
        return self._not_implemented("get_positions", positions=[])

    def get_orders(self) -> dict[str, Any]:
        """List recent orders (working + filled)."""
        return self._not_implemented("get_orders", orders=[])

    # ------------------------------------------------------------------
    # Mutating actions — placeholders today, real broker calls later.
    # ------------------------------------------------------------------

    def submit_market_order(self, signal: NormalizedSignal) -> dict[str, Any]:
        """Submit a market order based on a normalized signal.

        Default: not implemented. Adapters that do route market orders
        (e.g. paper) override this. Topstep/Tradovate keep the default
        so a misconfigured deploy never silently submits.
        """
        return self._not_implemented(
            "submit_market_order",
            symbol=signal.symbol,
            action=signal.action,
            contracts=signal.contracts,
        )

    def flatten_position(self, symbol: Optional[str] = None) -> dict[str, Any]:
        """Flatten one symbol or every open position."""
        return self._not_implemented("flatten_position", symbol=symbol)

    def cancel_all_orders(self, symbol: Optional[str] = None) -> dict[str, Any]:
        """Cancel working orders for one symbol or every symbol."""
        return self._not_implemented("cancel_all_orders", symbol=symbol)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _not_implemented(self, op: str, **extra: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": False,
            "provider": self.provider,
            "status": "not_implemented",
            "not_implemented": True,
            "message": f"{self.provider} {op} not implemented yet",
        }
        payload.update(extra)
        return payload
