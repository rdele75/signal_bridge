"""Paper-trading broker adapter.

Simulates accepted orders, tracks position state in memory and persists
the latest position snapshot to SQLite via the journal. When a fill
reduces a position (toward 0 or through it), basic realized PnL is
computed in price-points and recorded into ``closed_trades`` plus the
daily PnL bucket.
"""
from __future__ import annotations

import threading
import uuid
from typing import Any, Optional

from ..journal import Journal
from ..schemas import ExecutionResult, NormalizedSignal
from .broker_base import BrokerBase


class PaperBroker(BrokerBase):
    name = "paper"
    provider = "paper"
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

    def test_connection(self) -> dict[str, Any]:
        return {
            "ok": True,
            "provider": self.provider,
            "status": "ok",
            "message": "paper adapter ready",
        }

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
            new_qty, new_avg, new_side, msg, closed = self._apply(
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
            for trade in closed:
                self.journal.record_closed_trade(
                    symbol=signal.symbol,
                    side=trade["side"],
                    contracts=trade["contracts"],
                    entry_price=trade["entry_price"],
                    exit_price=trade["exit_price"],
                    realized_pnl_points=trade["pnl_points"],
                    broker_provider=self.provider,
                )
                self.journal.add_daily_pnl(trade["pnl_points"])

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
    ) -> tuple[int, Optional[float], Optional[str], str, list[dict[str, Any]]]:
        """Apply one fill to the current position. Returns the new
        (quantity, avg_price, side, message) plus a list of closed-trade
        records produced by any reducing/flattening portion of this fill.
        """
        qty = int(current.get("quantity") or 0)
        avg = current.get("avg_price")
        closed: list[dict[str, Any]] = []

        if action == "BUY":
            if qty >= 0:
                # Adding to long (or opening one).
                new_qty = qty + contracts
                new_avg = (
                    price
                    if qty == 0 or avg is None
                    else ((avg * qty) + (price * contracts)) / new_qty
                )
            else:
                # Buying against a short: closes some/all short contracts.
                closing = min(contracts, -qty)
                if avg is not None and closing > 0:
                    closed.append(
                        {
                            "side": "short",
                            "contracts": closing,
                            "entry_price": avg,
                            "exit_price": price,
                            "pnl_points": (avg - price) * closing,
                        }
                    )
                new_qty = qty + contracts
                if new_qty < 0:
                    new_avg = avg
                elif new_qty == 0:
                    new_avg = None
                else:
                    # Flipped through flat into a new long.
                    new_avg = price
            return new_qty, new_avg, _side_for(new_qty), "paper_filled_buy", closed

        if action == "SELL":
            # Reduce long (or flip into a short).
            if qty > 0:
                closing = min(contracts, qty)
                if avg is not None and closing > 0:
                    closed.append(
                        {
                            "side": "long",
                            "contracts": closing,
                            "entry_price": avg,
                            "exit_price": price,
                            "pnl_points": (price - avg) * closing,
                        }
                    )
            new_qty = qty - contracts
            if new_qty > 0:
                new_avg = avg
            elif new_qty == 0:
                new_avg = None
            else:
                # Flipped into short.
                new_avg = price if qty >= 0 else avg
            return new_qty, new_avg, _side_for(new_qty), "paper_filled_sell", closed

        if action == "SHORT":
            if qty <= 0:
                # Adding to short (or opening one).
                new_qty = qty - contracts
                new_avg = (
                    price
                    if qty == 0 or avg is None
                    else ((avg * abs(qty)) + (price * contracts)) / abs(new_qty)
                )
            else:
                # Selling against a long: closes some/all long contracts.
                closing = min(contracts, qty)
                if avg is not None and closing > 0:
                    closed.append(
                        {
                            "side": "long",
                            "contracts": closing,
                            "entry_price": avg,
                            "exit_price": price,
                            "pnl_points": (price - avg) * closing,
                        }
                    )
                new_qty = qty - contracts
                if new_qty > 0:
                    new_avg = avg
                elif new_qty == 0:
                    new_avg = None
                else:
                    new_avg = price
            return new_qty, new_avg, _side_for(new_qty), "paper_filled_short", closed

        if action == "COVER":
            # Covering a short.
            if qty < 0:
                closing = min(contracts, -qty)
                if avg is not None and closing > 0:
                    closed.append(
                        {
                            "side": "short",
                            "contracts": closing,
                            "entry_price": avg,
                            "exit_price": price,
                            "pnl_points": (avg - price) * closing,
                        }
                    )
            new_qty = qty + contracts
            if new_qty < 0:
                new_avg = avg
            elif new_qty == 0:
                new_avg = None
            else:
                new_avg = price
            return new_qty, new_avg, _side_for(new_qty), "paper_filled_cover", closed

        if action == "EXIT":
            # Flatten whatever's open.
            if qty != 0 and avg is not None:
                if qty > 0:
                    closed.append(
                        {
                            "side": "long",
                            "contracts": qty,
                            "entry_price": avg,
                            "exit_price": price,
                            "pnl_points": (price - avg) * qty,
                        }
                    )
                else:
                    closed.append(
                        {
                            "side": "short",
                            "contracts": -qty,
                            "entry_price": avg,
                            "exit_price": price,
                            "pnl_points": (avg - price) * (-qty),
                        }
                    )
            return 0, None, None, "paper_filled_exit", closed

        return qty, avg, _side_for(qty), f"paper_unknown_action:{action}", closed


def _side_for(qty: int) -> Optional[str]:
    if qty > 0:
        return "long"
    if qty < 0:
        return "short"
    return None
