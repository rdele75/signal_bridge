"""Paper-trading broker adapter.

Simulates accepted orders, tracks position state in memory and persists
the latest position snapshot to SQLite via the journal.
"""
from __future__ import annotations

import threading
import uuid
from typing import Optional

from ..journal import Journal
from ..schemas import ExecutionResult, NormalizedSignal
from .broker_base import BrokerBase


class PaperBroker(BrokerBase):
    name = "paper"
    execution_mode = "paper"

    def __init__(self, journal: Journal) -> None:
        self.journal = journal
        self._lock = threading.Lock()
        # In-memory mirror of the persisted position state, keyed by symbol.
        # Quantity is signed: positive = long, negative = short.
        self._positions: dict[str, dict] = {}
        self._hydrate()

    def _hydrate(self) -> None:
        for row in self.journal.list_open_positions():
            self._positions[row["symbol"]] = {
                "quantity": int(row["quantity"]),
                "avg_price": row.get("avg_price"),
                "side": row.get("side"),
            }

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def execute(self, signal: NormalizedSignal) -> ExecutionResult:
        if signal.price is None:
            return ExecutionResult(
                accepted=False,
                broker=self.name,
                execution_mode=self.execution_mode,
                symbol=signal.symbol,
                action=signal.action,
                contracts=signal.contracts,
                message="missing_or_invalid_price",
            )

        with self._lock:
            current = self._positions.get(
                signal.symbol, {"quantity": 0, "avg_price": None, "side": None}
            )
            new_qty, new_avg, new_side, msg = self._apply(
                signal.action,
                signal.contracts,
                signal.price,
                current,
            )
            self._positions[signal.symbol] = {
                "quantity": new_qty,
                "avg_price": new_avg,
                "side": new_side,
            }
            self.journal.upsert_position(
                symbol=signal.symbol,
                quantity=new_qty,
                avg_price=new_avg,
                side=new_side,
            )

        return ExecutionResult(
            accepted=True,
            broker=self.name,
            execution_mode=self.execution_mode,
            symbol=signal.symbol,
            action=signal.action,
            contracts=signal.contracts,
            fill_price=signal.price,
            order_id=signal.order_id or f"paper-{uuid.uuid4().hex[:12]}",
            message=msg,
            position_after={
                "symbol": signal.symbol,
                "quantity": new_qty,
                "avg_price": new_avg,
                "side": new_side,
            },
        )

    # ------------------------------------------------------------------
    # Position math
    # ------------------------------------------------------------------

    @staticmethod
    def _apply(
        action: str,
        contracts: int,
        price: float,
        current: dict,
    ) -> tuple[int, Optional[float], Optional[str], str]:
        qty = int(current.get("quantity") or 0)
        avg = current.get("avg_price")

        if action == "BUY":
            # Increase long (or reduce short).
            if qty >= 0:
                new_qty = qty + contracts
                new_avg = (
                    price
                    if qty == 0 or avg is None
                    else ((avg * qty) + (price * contracts)) / new_qty
                )
            else:
                new_qty = qty + contracts
                new_avg = avg if new_qty < 0 else (price if new_qty > 0 else None)
            return new_qty, new_avg, _side_for(new_qty), "paper_filled_buy"

        if action == "SELL":
            # Reduce long. Treat as a flat/reduce action in paper sim.
            new_qty = qty - contracts
            new_avg = avg if new_qty > 0 else (None if new_qty == 0 else price)
            return new_qty, new_avg, _side_for(new_qty), "paper_filled_sell"

        if action == "SHORT":
            if qty <= 0:
                new_qty = qty - contracts
                new_avg = (
                    price
                    if qty == 0 or avg is None
                    else ((avg * abs(qty)) + (price * contracts)) / abs(new_qty)
                )
            else:
                new_qty = qty - contracts
                new_avg = avg if new_qty > 0 else (price if new_qty < 0 else None)
            return new_qty, new_avg, _side_for(new_qty), "paper_filled_short"

        if action == "COVER":
            new_qty = qty + contracts
            new_avg = avg if new_qty < 0 else (None if new_qty == 0 else price)
            return new_qty, new_avg, _side_for(new_qty), "paper_filled_cover"

        if action == "EXIT":
            return 0, None, None, "paper_filled_exit"

        return qty, avg, _side_for(qty), f"paper_unknown_action:{action}"


def _side_for(qty: int) -> Optional[str]:
    if qty > 0:
        return "long"
    if qty < 0:
        return "short"
    return None
