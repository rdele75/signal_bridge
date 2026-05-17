"""Tradovate broker adapter — PLACEHOLDER.

Tradovate is a secondary planned broker target. Real order placement is
not implemented in this build. The adapter is instantiable so the app
can boot with BROKER_PROVIDER=tradovate, but `execute()` raises
NotImplementedError.

The webhook handler catches NotImplementedError and turns it into a
clearly-labeled rejection, so a misconfigured deploy can never silently
place a real order.
"""
from __future__ import annotations

from typing import Any

from ..schemas import ExecutionResult, NormalizedSignal
from .broker_base import BrokerBase


class TradovateBroker(BrokerBase):
    name = "tradovate"
    provider = "tradovate"
    execution_mode = "demo"

    def __init__(
        self,
        *,
        username: str = "",
        password: str = "",
        app_id: str = "",
        app_version: str = "",
        cid: str = "",
        sec: str = "",
        account_id: str = "",
        env: str = "demo",
    ) -> None:
        self.username = username
        self.password = password
        self.app_id = app_id
        self.app_version = app_version
        self.cid = cid
        self.sec = sec
        self.account_id = account_id
        self.env = env

    def test_connection(self) -> dict[str, Any]:
        return {
            "ok": False,
            "provider": self.provider,
            "status": "not_implemented",
            "message": (
                "tradovate test_connection not implemented yet — "
                "Tradovate adapter is a placeholder"
            ),
        }

    def execute(self, signal: NormalizedSignal) -> ExecutionResult:
        raise NotImplementedError(
            "tradovate_adapter_not_implemented: Tradovate live execution "
            "is a planned feature and not implemented yet. "
            "Use BROKER_PROVIDER=paper for now."
        )
