"""Tradovate live adapter — DISABLED PLACEHOLDER.

Live trading is intentionally not implemented. Any attempt to use this
adapter raises NotImplementedError so a misconfigured deploy can never
accidentally place a real order.
"""
from __future__ import annotations

from ..schemas import ExecutionResult, NormalizedSignal
from .broker_base import BrokerBase


class TradovateLiveBroker(BrokerBase):
    name = "tradovate_live"
    execution_mode = "live"

    def __init__(self, *, username: str = "", password: str = "", account_id: str = "") -> None:
        raise NotImplementedError(
            "Tradovate live execution is not implemented in SignalBridge yet. "
            "Set EXECUTION_MODE=paper and BROKER=paper."
        )

    def execute(self, signal: NormalizedSignal) -> ExecutionResult:  # pragma: no cover
        raise NotImplementedError("Tradovate live execution is not implemented.")
