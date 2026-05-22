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
    realized_pnl_dollars REAL,
    broker_provider TEXT,
    topstep_order_id TEXT
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
            closed_cols = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(closed_trades)")
            }
            for col, ddl in (
                (
                    "realized_pnl_dollars",
                    "ALTER TABLE closed_trades ADD COLUMN realized_pnl_dollars REAL",
                ),
                (
                    "topstep_order_id",
                    "ALTER TABLE closed_trades ADD COLUMN topstep_order_id TEXT",
                ),
            ):
                if col not in closed_cols:
                    conn.execute(ddl)
            # The unique index is declared in _SCHEMA but partial indexes
            # weren't supported on every prior SQLite; ensure it exists
            # after ALTERing in the column.
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS "
                "idx_closed_trades_topstep_order_id "
                "ON closed_trades(topstep_order_id) "
                "WHERE topstep_order_id IS NOT NULL"
            )

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

    # Action labels treated as entry-direction signals (open a position).
    # EXIT/COVER are exits and are matched against these via FIFO. Stored
    # uppercase to match the journal's normalized action labels.
    _ENTRY_ACTIONS: tuple[str, ...] = ("BUY", "SHORT", "SELL")

    def find_open_entry_for_symbol(
        self, symbol: str
    ) -> Optional[dict[str, Any]]:
        """Return the oldest accepted entry signal for ``symbol`` that
        has not yet been paired with a ``closed_trades`` row.

        Pairing model: closed_trades are recorded in submission order
        by the reactive + periodic reconciliation paths in the Topstep
        adapter. Each closed_trade row consumes one entry signal. So
        the next entry to consume is the (N+1)-th oldest entry, where
        N is the number of existing closes for the symbol. Returns
        ``None`` when there's no unmatched entry — the reconciler will
        log a WARNING and skip recording (a position opened directly
        on TopstepX, or a position already closed by a prior poll).
        """
        if not symbol:
            return None
        with self._lock, self._conn() as conn:
            placeholders = ",".join("?" * len(self._ENTRY_ACTIONS))
            entries = list(
                conn.execute(
                    f"""
                    SELECT id, received_at, symbol, action, contracts,
                           price, broker_provider, order_id
                    FROM signals
                    WHERE symbol = ? AND decision = 'accepted'
                      AND UPPER(action) IN ({placeholders})
                    ORDER BY id ASC
                    """,
                    (symbol, *self._ENTRY_ACTIONS),
                ).fetchall()
            )
            if not entries:
                return None
            cur = conn.execute(
                "SELECT COUNT(*) AS c FROM closed_trades WHERE symbol = ?",
                (symbol,),
            )
            closes_count = int(cur.fetchone()["c"])
            if closes_count >= len(entries):
                return None
            return dict(entries[closes_count])

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

    def get_daily_pnl_dollars(
        self, trade_date: Optional[str] = None
    ) -> float:
        """Sum today's ``realized_pnl_dollars`` from ``closed_trades``.

        The per-instrument USD multiplier is applied at reconciliation
        time (see ``app/execution/topstep.py``'s reactive and periodic
        close-trade paths) and persisted on each row, so this method
        is a straight SQL ``SUM`` over the trading-day window. Rows
        for symbols missing from ``INSTRUMENT_POINT_VALUES_USD`` land
        with ``realized_pnl_dollars=NULL`` and are silently excluded
        from the total — the WARNING is emitted once per close at
        reconciliation time so the operator can extend the table.

        ``trade_date`` is accepted for parity with ``get_daily_pnl``
        but the window is always derived from today's boundary in
        the configured trading-day timezone.
        """
        del trade_date  # signature parity with get_daily_pnl
        start_utc, end_utc = self._today_utc_window()
        with self._lock, self._conn() as conn:
            cur = conn.execute(
                """
                SELECT COALESCE(SUM(realized_pnl_dollars), 0.0) AS total
                FROM closed_trades
                WHERE closed_at >= ? AND closed_at < ?
                """,
                (start_utc, end_utc),
            )
            row = cur.fetchone()
            return float(row["total"] or 0.0) if row else 0.0

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
        realized_pnl_dollars: Optional[float] = None,
        broker_provider: Optional[str] = None,
        topstep_order_id: Optional[str] = None,
    ) -> int:
        # Auto-derive the dollar P&L from points × per-instrument
        # multiplier when the caller didn't supply one. Symbols not in
        # the multiplier table land with NULL and a one-shot WARNING so
        # the operator can extend it. The dashboard's dollar P&L card
        # and the daily-loss cap both read this column, so the single
        # write here keeps the two surfaces in sync.
        if realized_pnl_dollars is None:
            from .risk_engine import INSTRUMENT_POINT_VALUES_USD

            multiplier = INSTRUMENT_POINT_VALUES_USD.get(symbol)
            if multiplier is None:
                if realized_pnl_points != 0.0:
                    log.warning(
                        "record_closed_trade: no point value for symbol %r "
                        "— %.4f points won't contribute to dollar P&L or "
                        "the daily-loss cap. Add %r to "
                        "INSTRUMENT_POINT_VALUES_USD in app/risk_engine.py.",
                        symbol,
                        realized_pnl_points,
                        symbol,
                    )
            else:
                realized_pnl_dollars = float(realized_pnl_points) * float(multiplier)
        with self._lock, self._conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO closed_trades (
                    closed_at, symbol, side, contracts,
                    entry_price, exit_price, realized_pnl_points,
                    realized_pnl_dollars, broker_provider,
                    topstep_order_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _utcnow_iso(),
                    symbol,
                    side,
                    int(contracts),
                    entry_price,
                    exit_price,
                    float(realized_pnl_points),
                    (
                        float(realized_pnl_dollars)
                        if realized_pnl_dollars is not None
                        else None
                    ),
                    broker_provider,
                    topstep_order_id,
                ),
            )
            return int(cur.lastrowid)

    def closed_trade_exists_for_order_id(self, topstep_order_id: str) -> bool:
        """True iff a ``closed_trades`` row already carries this Topstep
        order id. Used by both the reactive (post-submit) and periodic
        reconciliation paths to avoid double-recording the same fill.
        Returns False when the id is empty so callers can pass through
        legacy paper-trade closes that have no broker order id."""
        if not topstep_order_id:
            return False
        with self._lock, self._conn() as conn:
            cur = conn.execute(
                "SELECT 1 FROM closed_trades WHERE topstep_order_id = ? LIMIT 1",
                (str(topstep_order_id),),
            )
            return cur.fetchone() is not None

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
                    COALESCE(SUM(realized_pnl_points), 0) AS total_points,
                    COALESCE(SUM(realized_pnl_dollars), 0) AS total_dollars
                FROM closed_trades
                """
            )
            row = cur.fetchone() or {}
            total = int(row["total"] or 0)
            wins = int(row["wins"] or 0)
            losses = int(row["losses"] or 0)
            total_points = float(row["total_points"] or 0.0)
            total_dollars = float(row["total_dollars"] or 0.0)
            return {
                "total": total,
                "wins": wins,
                "losses": losses,
                "total_points": total_points,
                "total_dollars": total_dollars,
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
