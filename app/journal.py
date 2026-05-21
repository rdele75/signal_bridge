"""SQLite-backed journal for signals, decisions, and executions."""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any, Iterable, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

log = logging.getLogger("signalbridge.journal")


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
    timeframe TEXT,
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


def _resolve_tz(name: Optional[str]) -> tzinfo:
    """Best-effort tz resolution. Falls back to UTC on missing tzdata or
    a bad name so the journal never refuses to compute a date bucket."""
    if not name or name.upper() == "UTC":
        return timezone.utc
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, KeyError) as exc:
        log.warning(
            "TRADING_DAY_TIMEZONE=%r could not be resolved (%s) — "
            "falling back to UTC for daily-PnL bucketing",
            name,
            exc.__class__.__name__,
        )
        return timezone.utc


class Journal:
    def __init__(
        self,
        db_path: str | Path,
        *,
        trading_day_timezone: str = "UTC",
    ) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        # The trading-day timezone controls the boundary for daily-PnL
        # buckets and "today" counts. UTC by default; operators trading
        # ES/NQ futures typically set this to ``America/New_York`` so
        # the day-rollover lines up with the local trading session
        # instead of 00:00 UTC (= 19:00 EST / 20:00 EDT, mid-session).
        self._trading_day_tz_name = trading_day_timezone or "UTC"
        self._trading_day_tz = _resolve_tz(self._trading_day_tz_name)
        self._init_schema()

    @property
    def trading_day_timezone(self) -> str:
        """Name of the configured trading-day timezone (for diagnostics)."""
        return self._trading_day_tz_name

    def _today_iso(self) -> str:
        """``YYYY-MM-DD`` for *now* in the configured trading-day tz."""
        return datetime.now(self._trading_day_tz).date().isoformat()

    def _today_utc_window(self) -> tuple[str, str]:
        """Return ``(start_utc_iso, end_utc_iso)`` covering the current
        trading day in the configured tz. Used to filter UTC-stored
        ``received_at`` timestamps against an operator-local day."""
        today_local = datetime.now(self._trading_day_tz).date()
        start_local = datetime.combine(
            today_local, datetime.min.time(), tzinfo=self._trading_day_tz
        )
        end_local = start_local + timedelta(days=1)
        return (
            start_local.astimezone(timezone.utc).isoformat(),
            end_local.astimezone(timezone.utc).isoformat(),
        )

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
                ("timeframe", "ALTER TABLE signals ADD COLUMN timeframe TEXT"),
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
        timeframe: Optional[str] = None,
    ) -> int:
        with self._lock, self._conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO signals (
                    received_at, source, strategy, symbol, action, contracts,
                    price, broker_provider, broker_symbol, order_id, timeframe,
                    raw_payload, decision, rejection_reason, execution_mode,
                    execution_result
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    timeframe,
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
            trade_date = self._today_iso()
        with self._lock, self._conn() as conn:
            cur = conn.execute(
                "SELECT realized_pnl FROM daily_pnl WHERE trade_date = ?",
                (trade_date,),
            )
            row = cur.fetchone()
            return float(row["realized_pnl"]) if row else 0.0

    def get_daily_pnl_dollars(self) -> float:
        """Return today's realized P&L converted to dollars per
        instrument's point value.

        Iterates today's ``closed_trades`` (UTC window aligned to the
        configured trading-day timezone), multiplies each row's
        ``realized_pnl_points`` by the per-instrument dollar value
        from ``INSTRUMENT_POINT_VALUES_USD``, and returns the sum.

        Symbols missing from the table contribute ``0.0`` and emit a
        single WARNING per unknown symbol per call — the daily-loss
        cap will under-count for those instruments, which is the safe
        direction for the operator to notice in the logs and fix by
        extending the table rather than for the cap to enforce the
        wrong number silently.
        """
        from .risk_engine import INSTRUMENT_POINT_VALUES_USD

        start_utc, end_utc = self._today_utc_window()
        with self._lock, self._conn() as conn:
            cur = conn.execute(
                """
                SELECT symbol, COALESCE(SUM(realized_pnl_points), 0.0) AS pts
                FROM closed_trades
                WHERE closed_at >= ? AND closed_at < ?
                GROUP BY symbol
                """,
                (start_utc, end_utc),
            )
            rows = [(r["symbol"], float(r["pts"] or 0.0)) for r in cur.fetchall()]

        total_dollars = 0.0
        for symbol, points in rows:
            multiplier = INSTRUMENT_POINT_VALUES_USD.get(symbol, 0.0)
            if multiplier == 0.0 and points != 0.0:
                log.warning(
                    "get_daily_pnl_dollars: no point value for symbol %r — "
                    "today's %.4f points contributes $0 to the daily loss "
                    "cap. Add %r to INSTRUMENT_POINT_VALUES_USD in "
                    "app/risk_engine.py if you trade it.",
                    symbol,
                    points,
                    symbol,
                )
                continue
            total_dollars += points * multiplier
        return total_dollars

    def add_daily_pnl(self, amount: float, trade_date: Optional[str] = None) -> None:
        if trade_date is None:
            trade_date = self._today_iso()
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
                       timeframe, decision, rejection_reason, execution_mode
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

    def count_today(
        self,
        *,
        decision: Optional[str] = None,
        execution_mode: Optional[str] = None,
    ) -> int:
        # ``received_at`` is stored in UTC. Use the operator-local
        # trading-day window so this count matches the daily-PnL bucket.
        start_utc, end_utc = self._today_utc_window()
        with self._lock, self._conn() as conn:
            params: list[Any] = [start_utc, end_utc]
            sql = (
                "SELECT COUNT(*) AS c FROM signals "
                "WHERE received_at >= ? AND received_at < ?"
            )
            if decision is not None:
                sql += " AND decision = ?"
                params.append(decision)
            if execution_mode is not None:
                sql += " AND execution_mode = ?"
                params.append(execution_mode)
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
