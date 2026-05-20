"""Paper-trading broker adapter.

Simulates accepted orders, tracks position state in memory and persists
the latest position snapshot to SQLite via the journal. When a fill
reduces a position (toward 0 or through it), basic realized PnL is
computed in price-points and recorded into ``closed_trades`` plus the
daily PnL bucket.

The paper adapter is the only fully-functional broker in this build.
Topstep is the scaffolded placeholder.
"""
from __future__ import annotations

import logging
import threading
import uuid
from typing import Any, Optional

from ..journal import Journal
from ..schemas import ExecutionResult, NormalizedSignal
from .broker_base import BrokerBase


DEFAULT_PAPER_ACCOUNT_ID = "PAPER-001"
DEFAULT_PAPER_BALANCE = 50_000.0

_log = logging.getLogger("signalbridge")


class PaperBroker(BrokerBase):
    name = "paper"
    provider = "paper"
    execution_mode = "paper"

    def __init__(
        self,
        journal: Journal,
        *,
        account_id: str = DEFAULT_PAPER_ACCOUNT_ID,
        starting_balance: float = DEFAULT_PAPER_BALANCE,
    ) -> None:
        self.journal = journal
        self.account_id = account_id or DEFAULT_PAPER_ACCOUNT_ID
        self.starting_balance = float(starting_balance)
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
    # Connection / accounts
    # ------------------------------------------------------------------

    def test_connection(self) -> dict[str, Any]:
        return {
            "ok": True,
            "provider": self.provider,
            "status": "ok",
            "not_implemented": False,
            "message": "paper adapter ready",
            "account_id": self.account_id,
            "execution_mode": self.execution_mode,
        }

    def _account_snapshot(self) -> dict[str, Any]:
        stats = self.journal.closed_trade_stats()
        realized = float(stats.get("total_points") or 0.0)
        return {
            "account_id": self.account_id,
            "provider": self.provider,
            "name": "SignalBridge Paper",
            "currency": "USD",
            "balance": self.starting_balance,
            "equity": self.starting_balance + realized,
            "realized_pnl_points": realized,
            "daily_pnl_points": self.journal.get_daily_pnl(),
            "open_position_count": self.journal.count_open_positions(),
            "is_simulated": True,
        }

    def get_accounts(self) -> dict[str, Any]:
        account = self._account_snapshot()
        return {
            "ok": True,
            "provider": self.provider,
            "not_implemented": False,
            "accounts": [account],
            "selected_account_id": self.account_id,
        }

    def get_selected_account(self) -> dict[str, Any]:
        account = self._account_snapshot()
        return {
            "ok": True,
            "provider": self.provider,
            "not_implemented": False,
            "selected_account_id": self.account_id,
            "account": account,
        }

    def get_positions(self) -> dict[str, Any]:
        rows = self.journal.list_open_positions()
        return {
            "ok": True,
            "provider": self.provider,
            "not_implemented": False,
            "account_id": self.account_id,
            "positions": [
                {
                    "symbol": r["symbol"],
                    "side": r.get("side"),
                    "quantity": int(r.get("quantity") or 0),
                    "avg_price": r.get("avg_price"),
                    "updated_at": r.get("updated_at"),
                }
                for r in rows
            ],
        }

    def get_orders(self) -> dict[str, Any]:
        rows = self.journal.list_recent_signals(limit=25)
        orders = []
        for r in rows:
            orders.append(
                {
                    "order_id": r.get("order_id"),
                    "received_at": r.get("received_at"),
                    "symbol": r.get("symbol"),
                    "broker_symbol": r.get("broker_symbol"),
                    "action": r.get("action"),
                    "contracts": r.get("contracts"),
                    "price": r.get("price"),
                    "decision": r.get("decision"),
                    "rejection_reason": r.get("rejection_reason"),
                    "execution_mode": r.get("execution_mode"),
                    "broker_provider": r.get("broker_provider"),
                }
            )
        return {
            "ok": True,
            "provider": self.provider,
            "not_implemented": False,
            "account_id": self.account_id,
            "orders": orders,
        }

    # ------------------------------------------------------------------
    # Order entry
    # ------------------------------------------------------------------

    def submit_market_order(self, signal: NormalizedSignal) -> dict[str, Any]:
        result = self.execute(signal)
        return {
            "ok": result.accepted,
            "provider": self.provider,
            "not_implemented": False,
            "account_id": self.account_id,
            "result": result.model_dump(),
        }

    def flatten_position(self, symbol: Optional[str] = None) -> dict[str, Any]:
        # Paper "flatten" just zeroes out the in-memory + persisted
        # position state without trying to compute a fair exit price —
        # there is no live market context.
        flattened: list[str] = []
        # L3 — distinguish "asked for a symbol we don't track" from
        # "the symbol is tracked but already flat" so the message
        # surfaces the actual state instead of a generic count.
        unknown_symbol = symbol is not None and symbol not in self._positions
        with self._lock:
            targets = [symbol] if symbol else list(self._positions.keys())
            for sym in targets:
                if sym not in self._positions:
                    continue
                if int(self._positions[sym].get("quantity") or 0) == 0:
                    # Already flat — skip so we don't claim phantom flattens.
                    continue
                self._positions[sym] = {
                    "quantity": 0,
                    "avg_price": None,
                    "side": None,
                }
                self.journal.upsert_position(
                    symbol=sym, quantity=0, avg_price=None, side=None
                )
                flattened.append(sym)
        event = "paper_flatten_symbol" if symbol else "paper_flatten_all"
        _log.info(
            "%s account=%s symbol=%s flattened=%s",
            event,
            self.account_id,
            symbol or "*",
            flattened,
        )
        if flattened:
            message = f"flattened {len(flattened)} position(s)"
        elif unknown_symbol:
            message = (
                f"no open position for symbol {symbol} — nothing to flatten"
            )
        else:
            message = "no open positions"
        return {
            "ok": True,
            "provider": self.provider,
            "not_implemented": False,
            "account_id": self.account_id,
            "event": event,
            "symbol": symbol,
            "flattened": flattened,
            "count": len(flattened),
            "message": message,
        }

    def flatten_all_positions(self) -> dict[str, Any]:
        """Close/zero every open simulated position. Returns structured JSON."""
        return self.flatten_position(symbol=None)

    def reset_paper_state(self) -> dict[str, Any]:
        """Clear paper open positions and in-memory order state.

        Does NOT delete the signal journal, closed-trade history, or
        daily PnL — those are operator records, not paper runtime state.
        """
        cleared: list[str] = []
        with self._lock:
            for sym, pos in list(self._positions.items()):
                if int(pos.get("quantity") or 0) != 0:
                    cleared.append(sym)
                self.journal.upsert_position(
                    symbol=sym, quantity=0, avg_price=None, side=None
                )
            self._positions = {}
        _log.info(
            "paper_reset_state account=%s cleared=%s",
            self.account_id,
            cleared,
        )
        return {
            "ok": True,
            "provider": self.provider,
            "not_implemented": False,
            "account_id": self.account_id,
            "event": "paper_reset_state",
            "cleared_symbols": cleared,
            "count": len(cleared),
            "message": (
                f"reset paper state; {len(cleared)} open position(s) zeroed"
                if cleared
                else "reset paper state; no open positions"
            ),
        }

    def cancel_all_orders(self, symbol: Optional[str] = None) -> dict[str, Any]:
        # Paper fills synchronously, so there are never working orders.
        return {
            "ok": True,
            "provider": self.provider,
            "not_implemented": False,
            "account_id": self.account_id,
            "cancelled": [],
            "message": "paper has no working orders to cancel",
        }

    # ------------------------------------------------------------------
    # Execute (legacy entry point used by webhook handler)
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
