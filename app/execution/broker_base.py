"""Abstract broker adapter interface."""
from __future__ import annotations

from abc import ABC, abstractmethod

from ..schemas import ExecutionResult, NormalizedSignal


class BrokerBase(ABC):
    """All broker adapters implement this interface."""

    name: str = "base"
    execution_mode: str = "paper"

    @abstractmethod
    def execute(self, signal: NormalizedSignal) -> ExecutionResult:
        """Execute a normalized signal and return an ExecutionResult."""
        raise NotImplementedError
