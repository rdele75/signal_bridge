"""SQLite-backed journal for signals, decisions, and executions."""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional


_SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    received_at TEXT NOT NULL,
    source TEXT,
    strategy TEXT,
    symbol TEXT,
    action TEXT,
    contracts INTEGER,
    price REAL,
    order_id TEXT,
    raw_payload TEXT,
    decision TEXT,
    rejection_reason TEXT,
    execution_mode TEXT,
    execution_result TEXT
);

CREATE INDEX IF NOT EXISTS idx_signals_order_id ON signals(order_id);
CREATE INDEX IF NOT EXISTS idx_signals_received_at ON signals(received_at);

CREATE TABLE IF NOT EXISTS positions (
    symbol TEXT PRIMARY KEY,
    quantity INTEGER NOT NULL,
    avg_price REAL,
    side TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS daily_pnl (
    trade_date TEXT PRIMARY KEY,
    realized_pnl REAL NOT NULL DEFAULT 0
);
"""


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Journal:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_schema()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._lock, self._conn() as conn:
            conn.executescript(_SCHEMA)

    # ----- Signal log -----

    def record_signal(
        self,
        *,
        source: Optional[str],
        strategy: Optional[str],
        symbol: Optional[str],
        action: Optional[str],
        contracts: Optional[int],
        price: Optional[float],
        order_id: Optional[str],
        raw_payload: dict[str, Any],
        decision: str,
        rejection_reason: Optional[str],
        execution_mode: str,
        execution_result: Optional[dict[str, Any]] = None,
    ) -> int:
        with self._lock, self._conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO signals (
                    received_at, source, strategy, symbol, action, contracts,
                    price, order_id, raw_payload, decision, rejection_reason,
                    execution_mode, execution_result
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _utcnow_iso(),
                    source,
                    strategy,
                    symbol,
                    action,
                    contracts,
                    price,
                    order_id,
                    json.dumps(raw_payload, default=str),
                    decision,
                    rejection_reason,
                    execution_mode,
                    json.dumps(execution_result, default=str)
                    if execution_result is not None
                    else None,
                ),
            )
            return int(cur.lastrowid)

    def find_recent_order_id(
        self, order_id: str, *, within_seconds: int
    ) -> Optional[sqlite3.Row]:
        """Return the most recent accepted signal with this order_id inside
        the cooldown window, if any."""
        if not order_id:
            return None
        with self._lock, self._conn() as conn:
            cur = conn.execute(
                """
                SELECT * FROM signals
                WHERE order_id = ?
                  AND decision = 'accepted'
                  AND received_at >= datetime('now', ?)
                ORDER BY id DESC
                LIMIT 1
                """,
                (order_id, f"-{int(within_seconds)} seconds"),
            )
            return cur.fetchone()

    # ----- Positions -----

    def get_position(self, symbol: str) -> Optional[dict[str, Any]]:
        with self._lock, self._conn() as conn:
            cur = conn.execute("SELECT * FROM positions WHERE symbol = ?", (symbol,))
            row = cur.fetchone()
            return dict(row) if row else None

    def list_open_positions(self) -> list[dict[str, Any]]:
        with self._lock, self._conn() as conn:
            cur = conn.execute(
                "SELECT * FROM positions WHERE quantity != 0 ORDER BY symbol"
            )
            return [dict(r) for r in cur.fetchall()]

    def count_open_positions(self) -> int:
        with self._lock, self._conn() as conn:
            cur = conn.execute(
                "SELECT COUNT(*) AS c FROM positions WHERE quantity != 0"
            )
            return int(cur.fetchone()["c"])

    def upsert_position(
        self,
        *,
        symbol: str,
        quantity: int,
        avg_price: Optional[float],
        side: Optional[str],
    ) -> None:
        with self._lock, self._conn() as conn:
            conn.execute(
                """
                INSERT INTO positions (symbol, quantity, avg_price, side, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(symbol) DO UPDATE SET
                    quantity = excluded.quantity,
                    avg_price = excluded.avg_price,
                    side = excluded.side,
                    updated_at = excluded.updated_at
                """,
                (symbol, quantity, avg_price, side, _utcnow_iso()),
            )

    # ----- Daily PnL -----

    def get_daily_pnl(self, trade_date: Optional[str] = None) -> float:
        if trade_date is None:
            trade_date = datetime.now(timezone.utc).date().isoformat()
        with self._lock, self._conn() as conn:
            cur = conn.execute(
                "SELECT realized_pnl FROM daily_pnl WHERE trade_date = ?",
                (trade_date,),
            )
            row = cur.fetchone()
            return float(row["realized_pnl"]) if row else 0.0

    def add_daily_pnl(self, amount: float, trade_date: Optional[str] = None) -> None:
        if trade_date is None:
            trade_date = datetime.now(timezone.utc).date().isoformat()
        with self._lock, self._conn() as conn:
            conn.execute(
                """
                INSERT INTO daily_pnl (trade_date, realized_pnl)
                VALUES (?, ?)
                ON CONFLICT(trade_date) DO UPDATE SET
                    realized_pnl = daily_pnl.realized_pnl + excluded.realized_pnl
                """,
                (trade_date, amount),
            )

    def reset(self) -> None:
        """Wipe all tables. Test/dev helper."""
        with self._lock, self._conn() as conn:
            conn.executescript(
                "DELETE FROM signals; DELETE FROM positions; DELETE FROM daily_pnl;"
            )
