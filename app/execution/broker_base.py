"""Abstract broker adapter interface."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

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

    def test_connection(self) -> dict[str, Any]:
        """Readiness probe for the dashboard "Test connection" button.

        Default returns "not implemented" so adapters that don't override
        this can't accidentally claim they work.
        """
        return {
            "ok": False,
            "provider": self.provider,
            "status": "not_implemented",
            "message": f"{self.provider} test_connection not implemented yet",
        }
