"""Dashboard summary builder + per-page context helpers.

Keeps the FastAPI route functions thin — each one calls into here for the
data it needs, then hands the dict straight to Jinja2.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from .config import Settings
from .execution.broker_base import BrokerBase
from .journal import Journal
from .kill_switch import KillSwitch


def _maybe_json(value: Optional[str]) -> Any:
    if not value:
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value


def dashboard_summary(
    *,
    settings: Settings,
    journal: Journal,
    kill_switch: KillSwitch,
    broker: BrokerBase,
) -> dict[str, Any]:
    open_positions = journal.list_open_positions()
    accepted_today = journal.count_today(decision="accepted")
    rejected_today = journal.count_today(decision="rejected")
    last_signal = journal.latest_signal()
    last_rejection = journal.latest_signal(decision="rejected")
    daily_pnl = journal.get_daily_pnl()
    closed = journal.closed_trade_stats()

    pnl_display = (
        f"{daily_pnl:+.2f} pts" if closed["total"] > 0 else "N/A"
    )

    return {
        "app_name": settings.app_name,
        "execution_mode": settings.execution_mode,
        "broker_provider": broker.provider,
        "broker_account_id": _broker_account_id(settings, broker.provider),
        "kill_switch_active": kill_switch.is_active(),
        "kill_switch_enabled": kill_switch.enabled,
        "allowed_symbols": list(settings.allowed_symbols),
        "open_positions": open_positions,
        "open_position_count": len(open_positions),
        "trades_today": accepted_today,
        "accepted_today": accepted_today,
        "rejected_today": rejected_today,
        "last_signal": _signal_for_display(last_signal),
        "last_rejection": _signal_for_display(last_rejection),
        "daily_pnl": daily_pnl,
        "daily_pnl_display": pnl_display,
        "closed_trade_total": closed["total"],
    }


def metrics_summary(*, journal: Journal) -> dict[str, Any]:
    closed = journal.closed_trade_stats()
    win_rate = (
        f"{(closed['wins'] / closed['total']) * 100:.1f}%"
        if closed["total"] >= 3
        else "N/A"
    )
    return {
        "accepted_today": journal.count_today(decision="accepted"),
        "rejected_today": journal.count_today(decision="rejected"),
        "total_today": journal.count_today(),
        "rejection_reasons": journal.rejection_reasons(limit=20),
        "trades_by_symbol": journal.trades_by_symbol(),
        "open_positions": journal.list_open_positions(),
        "closed_trades": journal.list_recent_closed_trades(limit=25),
        "closed_total": closed["total"],
        "closed_wins": closed["wins"],
        "closed_losses": closed["losses"],
        "total_points": closed["total_points"],
        "win_rate": win_rate,
        "daily_pnl": journal.get_daily_pnl(),
    }


def journal_view(*, journal: Journal, limit: int = 100) -> dict[str, Any]:
    return {
        "signals": journal.list_recent_signals(limit=limit),
        "closed_trades": journal.list_recent_closed_trades(limit=limit),
    }


def _signal_for_display(row: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    if not row:
        return None
    out = dict(row)
    out["raw_payload"] = _maybe_json(out.get("raw_payload"))
    out["execution_result"] = _maybe_json(out.get("execution_result"))
    return out


def _broker_account_id(settings: Settings, provider: str) -> Optional[str]:
    if provider == "topstep":
        return settings.topstep_account_id or None
    if provider == "tradovate":
        return settings.tradovate_account_id or None
    return None


# ----- Logs page helpers -----

def tail_log(path: Path, *, lines: int = 200) -> list[str]:
    """Return the last ~`lines` lines of the log file. Returns an empty
    list when the log doesn't exist yet."""
    if not path.exists():
        return []
    try:
        # Read the tail without loading the whole file when it's large.
        with path.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            block = 8192
            data = b""
            while size > 0 and data.count(b"\n") <= lines:
                read = min(block, size)
                size -= read
                f.seek(size)
                data = f.read(read) + data
        text = data.decode("utf-8", errors="replace")
    except OSError:
        return []
    return text.splitlines()[-lines:]
