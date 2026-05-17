"""Topstep / TopstepX broker adapter — PLACEHOLDER.

Topstep is the primary planned live broker target for SignalBridge, but
real order placement is not implemented in this build. The adapter is
instantiable so the app can boot with BROKER_PROVIDER=topstep, but any
attempt to actually `execute()` a signal raises NotImplementedError.

The webhook handler catches NotImplementedError and turns it into a
clearly-labeled rejection, so a misconfigured deploy can never silently
place a real order.
"""
from __future__ import annotations

from typing import Any

from ..schemas import ExecutionResult, NormalizedSignal
from .broker_base import BrokerBase


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

    def test_connection(self) -> dict[str, Any]:
        return {
            "ok": False,
            "provider": self.provider,
            "status": "not_implemented",
            "message": (
                "topstep test_connection not implemented yet — "
                "Topstep / TopstepX adapter is a placeholder"
            ),
        }

    def execute(self, signal: NormalizedSignal) -> ExecutionResult:
        raise NotImplementedError(
            "topstep_adapter_not_implemented: Topstep / TopstepX live "
            "execution is a planned feature and not implemented yet. "
            "Use BROKER_PROVIDER=paper for now."
        )
