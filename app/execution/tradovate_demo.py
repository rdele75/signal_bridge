"""Tradovate demo (paper) adapter — placeholder interface.

This is a stub for a future Tradovate demo-account integration. It does
not place real orders. Calling `execute` returns an accepted=False
result with a clear message so a user accidentally wiring this in does
not silently no-op.
"""
from __future__ import annotations

from ..schemas import ExecutionResult, NormalizedSignal
from .broker_base import BrokerBase


class TradovateDemoBroker(BrokerBase):
    name = "tradovate_demo"
    execution_mode = "demo"

    def __init__(self, *, username: str = "", password: str = "", account_id: str = "") -> None:
        self.username = username
        self.password = password
        self.account_id = account_id

    def execute(self, signal: NormalizedSignal) -> ExecutionResult:
        return ExecutionResult(
            accepted=False,
            broker=self.name,
            execution_mode=self.execution_mode,
            symbol=signal.symbol,
            action=signal.action,
            contracts=signal.contracts,
            message=(
                "tradovate_demo_not_implemented: this adapter is a placeholder. "
                "Use BROKER=paper for now."
            ),
        )
