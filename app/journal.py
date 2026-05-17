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
    broker_provider TEXT,
    broker_symbol TEXT,
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

CREATE TABLE IF NOT EXISTS closed_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    closed_at TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    contracts INTEGER NOT NULL,
    entry_price REAL,
    exit_price REAL,
    realized_pnl_points REAL NOT NULL DEFAULT 0,
    broker_provider TEXT
);

CREATE INDEX IF NOT EXISTS idx_closed_trades_closed_at ON closed_trades(closed_at);
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
            # Add columns that may be missing on older databases. SQLite
            # has no IF NOT EXISTS for ADD COLUMN, so we check pragma first.
            existing = {row["name"] for row in conn.execute("PRAGMA table_info(signals)")}
            for col, ddl in (
                ("broker_provider", "ALTER TABLE signals ADD COLUMN broker_provider TEXT"),
                ("broker_symbol", "ALTER TABLE signals ADD COLUMN broker_symbol TEXT"),
            ):
                if col not in existing:
                    conn.execute(ddl)

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
        broker_provider: Optional[str] = None,
        broker_symbol: Optional[str] = None,
    ) -> int:
        with self._lock, self._conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO signals (
                    received_at, source, strategy, symbol, action, contracts,
                    price, broker_provider, broker_symbol, order_id, raw_payload,
                    decision, rejection_reason, execution_mode, execution_result
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _utcnow_iso(),
                    source,
                    strategy,
                    symbol,
                    action,
                    contracts,
                    price,
                    broker_provider,
                    broker_symbol,
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

    # ----- Closed trades (basic paper PnL in price-points) -----

    def record_closed_trade(
        self,
        *,
        symbol: str,
        side: str,
        contracts: int,
        entry_price: Optional[float],
        exit_price: Optional[float],
        realized_pnl_points: float,
        broker_provider: Optional[str] = None,
    ) -> int:
        with self._lock, self._conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO closed_trades (
                    closed_at, symbol, side, contracts,
                    entry_price, exit_price, realized_pnl_points,
                    broker_provider
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _utcnow_iso(),
                    symbol,
                    side,
                    int(contracts),
                    entry_price,
                    exit_price,
                    float(realized_pnl_points),
                    broker_provider,
                ),
            )
            return int(cur.lastrowid)

    def list_recent_closed_trades(self, *, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock, self._conn() as conn:
            cur = conn.execute(
                "SELECT * FROM closed_trades ORDER BY id DESC LIMIT ?",
                (int(limit),),
            )
            return [dict(r) for r in cur.fetchall()]

    def closed_trade_stats(self) -> dict[str, Any]:
        with self._lock, self._conn() as conn:
            cur = conn.execute(
                """
                SELECT
                    COUNT(*)               AS total,
                    SUM(CASE WHEN realized_pnl_points > 0 THEN 1 ELSE 0 END) AS wins,
                    SUM(CASE WHEN realized_pnl_points < 0 THEN 1 ELSE 0 END) AS losses,
                    COALESCE(SUM(realized_pnl_points), 0) AS total_points
                FROM closed_trades
                """
            )
            row = cur.fetchone() or {}
            total = int(row["total"] or 0)
            wins = int(row["wins"] or 0)
            losses = int(row["losses"] or 0)
            total_points = float(row["total_points"] or 0.0)
            return {
                "total": total,
                "wins": wins,
                "losses": losses,
                "total_points": total_points,
            }

    # ----- Reporting / dashboard aggregations -----

    def list_recent_signals(self, *, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock, self._conn() as conn:
            cur = conn.execute(
                """
                SELECT id, received_at, source, strategy, symbol, broker_symbol,
                       action, contracts, price, broker_provider, order_id,
                       decision, rejection_reason, execution_mode
                FROM signals
                ORDER BY id DESC
                LIMIT ?
                """,
                (int(limit),),
            )
            return [dict(r) for r in cur.fetchall()]

    def latest_signal(self, *, decision: Optional[str] = None) -> Optional[dict[str, Any]]:
        sql = (
            "SELECT * FROM signals "
            + ("WHERE decision = ? " if decision else "")
            + "ORDER BY id DESC LIMIT 1"
        )
        with self._lock, self._conn() as conn:
            cur = conn.execute(sql, (decision,) if decision else ())
            row = cur.fetchone()
            return dict(row) if row else None

    def count_today(self, *, decision: Optional[str] = None) -> int:
        with self._lock, self._conn() as conn:
            params: list[Any] = []
            sql = (
                "SELECT COUNT(*) AS c FROM signals "
                "WHERE date(received_at) = date('now')"
            )
            if decision is not None:
                sql += " AND decision = ?"
                params.append(decision)
            cur = conn.execute(sql, params)
            return int(cur.fetchone()["c"])

    def rejection_reasons(self, *, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock, self._conn() as conn:
            cur = conn.execute(
                """
                SELECT rejection_reason AS reason, COUNT(*) AS count
                FROM signals
                WHERE decision = 'rejected' AND rejection_reason IS NOT NULL
                GROUP BY rejection_reason
                ORDER BY count DESC
                LIMIT ?
                """,
                (int(limit),),
            )
            return [dict(r) for r in cur.fetchall()]

    def trades_by_symbol(self) -> list[dict[str, Any]]:
        with self._lock, self._conn() as conn:
            cur = conn.execute(
                """
                SELECT symbol,
                       SUM(CASE WHEN decision='accepted' THEN 1 ELSE 0 END) AS accepted,
                       SUM(CASE WHEN decision='rejected' THEN 1 ELSE 0 END) AS rejected,
                       COUNT(*) AS total
                FROM signals
                WHERE symbol IS NOT NULL
                GROUP BY symbol
                ORDER BY total DESC
                """
            )
            return [dict(r) for r in cur.fetchall()]

    def reset(self) -> None:
        """Wipe all tables. Test/dev helper."""
        with self._lock, self._conn() as conn:
            conn.executescript(
                "DELETE FROM signals; "
                "DELETE FROM positions; "
                "DELETE FROM daily_pnl; "
                "DELETE FROM closed_trades;"
            )
