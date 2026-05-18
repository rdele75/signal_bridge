"""Dashboard summary builder + per-page context helpers.

Keeps the FastAPI route functions thin — each one calls into here for the
data it needs, then hands the dict straight to Jinja2.
"""
from __future__ import annotations

import json
import os
import platform
import socket
import sys
from pathlib import Path
from typing import Any, Optional

from . import __version__
from .config import PROJECT_ROOT, Settings
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

    webhook = _webhook_status(settings)
    configured_provider = settings.resolved_provider
    broker_status = broker_status_payload(settings=settings, broker=broker)

    # Recent paper orders for the dashboard table. Only paper has fills
    # right now — for placeholder providers the list comes back empty
    # but still renders safely.
    recent_orders_resp = _safe_get_orders(broker)
    recent_orders = recent_orders_resp.get("orders") or []

    broker_positions_resp = _safe_get_positions(broker)
    broker_positions = broker_positions_resp.get("positions") or []
    broker_orders = recent_orders
    broker_account_card = _broker_account_card(
        broker_status=broker_status,
        positions_resp=broker_positions_resp,
        orders_resp=recent_orders_resp,
        positions=broker_positions,
        orders=broker_orders,
    )

    return {
        "app_name": settings.app_name,
        "app_version": __version__,
        "execution_mode": settings.execution_mode,
        "broker_provider": configured_provider,
        "active_broker_provider": broker.provider,
        "broker_account_id": _broker_account_id(settings, configured_provider),
        "selected_account_id": settings.resolved_account_id or None,
        "broker_status": broker_status,
        "broker_connected": broker_status["broker_connected"],
        "broker_message": broker_status["broker_message"],
        "broker_not_implemented": broker_status["not_implemented"],
        "broker_account_card": broker_account_card,
        "recent_orders": recent_orders[:10],
        "recent_orders_not_implemented": bool(
            recent_orders_resp.get("not_implemented")
        ),
        "kill_switch_active": kill_switch.is_active(),
        "kill_switch_enabled": kill_switch.enabled,
        "allowed_symbols": list(settings.allowed_symbols),
        "timeframe_lock_enabled": settings.enable_timeframe_lock,
        "allowed_timeframes": list(settings.allowed_timeframes),
        "allowed_timeframes_csv": ",".join(settings.allowed_timeframes),
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
        "webhook_path": "/webhooks/tradingview",
        "webhook_secret_set": webhook["secret_set"],
        "webhook_url_local": webhook["url_local"],
    }


def _safe_get_orders(broker: BrokerBase) -> dict[str, Any]:
    try:
        result = broker.get_orders()
    except Exception:  # pragma: no cover - defensive
        return {"ok": False, "orders": [], "not_implemented": False}
    return result if isinstance(result, dict) else {"ok": False, "orders": []}


def _safe_get_positions(broker: BrokerBase) -> dict[str, Any]:
    try:
        result = broker.get_positions()
    except Exception:  # pragma: no cover - defensive
        return {"ok": False, "positions": [], "not_implemented": False}
    return (
        result
        if isinstance(result, dict)
        else {"ok": False, "positions": []}
    )


def _broker_account_card(
    *,
    broker_status: dict[str, Any],
    positions_resp: dict[str, Any],
    orders_resp: dict[str, Any],
    positions: list[Any],
    orders: list[Any],
) -> dict[str, Any]:
    """Compact summary card the dashboard renders for the active broker
    account. Surfaces what's reliably available; any field the adapter
    doesn't expose is ``None`` and the template renders a dash."""
    return {
        "provider": broker_status.get("provider"),
        "broker_provider": broker_status.get("broker_provider"),
        "broker_connected": broker_status.get("broker_connected"),
        "broker_message": broker_status.get("broker_message"),
        "status": broker_status.get("status"),
        "auth_status": broker_status.get("auth_status"),
        "not_implemented": broker_status.get("not_implemented"),
        "selected_account_id": broker_status.get("selected_account_id"),
        "selected_account_name": broker_status.get("selected_account_name"),
        "balance": broker_status.get("balance"),
        "can_trade": broker_status.get("can_trade"),
        "is_visible": broker_status.get("is_visible"),
        "token_cached": broker_status.get("token_cached"),
        "token_expires_at": broker_status.get("token_expires_at"),
        "positions_count": len(positions),
        "positions_not_implemented": bool(positions_resp.get("not_implemented")),
        "positions_message": positions_resp.get("message", ""),
        "orders_count": len(orders),
        "orders_not_implemented": bool(orders_resp.get("not_implemented")),
        "orders_message": orders_resp.get("message", ""),
    }


def _webhook_status(settings: Settings) -> dict[str, Any]:
    secret = settings.webhook_secret or ""
    secret_set = bool(secret) and secret != "change_me_to_a_long_random_secret"
    return {
        "secret_set": secret_set,
        "url_local": f"http://{settings.app_host}:{settings.app_port}/webhooks/tradingview",
    }


def system_summary(
    *,
    settings: Settings,
    broker: BrokerBase,
    kill_switch: KillSwitch,
) -> dict[str, Any]:
    """Build the System page payload. Used by both /system (HTML) and
    /api/system (JSON)."""
    env_file = PROJECT_ROOT / ".env"
    webhook = _webhook_status(settings)
    host = settings.app_host
    port = settings.app_port

    local_urls = [
        {"label": "Dashboard", "url": f"http://{host}:{port}/"},
        {"label": "System",    "url": f"http://{host}:{port}/system"},
        {"label": "Broker",    "url": f"http://{host}:{port}/settings/broker"},
        {"label": "Risk",      "url": f"http://{host}:{port}/settings/risk"},
        {"label": "TradingView", "url": f"http://{host}:{port}/tradingview"},
        {"label": "Journal",   "url": f"http://{host}:{port}/journal"},
        {"label": "Metrics",   "url": f"http://{host}:{port}/metrics"},
        {"label": "Logs",      "url": f"http://{host}:{port}/logs"},
        {"label": "Health",    "url": f"http://{host}:{port}/health"},
        {"label": "Webhook",   "url": webhook["url_local"]},
    ]

    return {
        "app_name": settings.app_name,
        "app_version": __version__,
        "host": host,
        "port": port,
        "execution_mode": settings.execution_mode,
        "broker_provider": settings.resolved_provider,
        "active_broker_provider": broker.provider,
        "database_path": str(settings.database_abs_path),
        "log_path": str(settings.log_abs_path),
        "log_level": settings.log_level,
        "cwd": str(Path.cwd()),
        "project_root": str(PROJECT_ROOT),
        "env_file_path": str(env_file),
        "env_file_loaded": env_file.exists(),
        "webhook_path": "/webhooks/tradingview",
        "webhook_url_local": webhook["url_local"],
        "webhook_secret_set": webhook["secret_set"],
        "kill_switch_active": kill_switch.is_active(),
        "kill_switch_enabled": kill_switch.enabled,
        "runtime_status": "halted" if kill_switch.is_active() else "running",
        "python_version": sys.version.split()[0],
        "platform": platform.platform(),
        "hostname": socket.gethostname(),
        "pid": os.getpid(),
        "local_urls": local_urls,
        "tailscale_note": (
            "If you're on a Tailscale network, reach the dashboard at "
            "http://<this-machine-tailscale-name>:"
            f"{port}/. Do not hardcode IPs — use the magic DNS name."
        ),
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
    resolved = settings.resolved_account_id
    return resolved or None


_TOKEN_EXPIRY_VISIBLE_CHARS = 19  # "YYYY-MM-DDTHH:MM:SS"


def _mask_token_expiry(value: Optional[str]) -> str:
    """Trim a cached token's ISO expiry to a stable, dashboard-safe
    prefix. Empty values come back empty."""
    if not value:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    return text[:_TOKEN_EXPIRY_VISIBLE_CHARS]


def _account_view(account: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Project a Topstep account into the dashboard-friendly subset.

    Returns ``None`` when no account is supplied so the template can
    cleanly render "no selected account" without poking into a dict.
    """
    if not isinstance(account, dict):
        return None
    raw_id = account.get("id", account.get("account_id"))
    return {
        "id": raw_id,
        "account_id": raw_id,
        "id_str": "" if raw_id is None else str(raw_id),
        "name": account.get("name"),
        "balance": account.get("balance"),
        "can_trade": account.get("can_trade"),
        "is_visible": account.get("is_visible"),
    }


def broker_status_payload(
    *, settings: Settings, broker: BrokerBase
) -> dict[str, Any]:
    """Snapshot used by /api/broker/status and the dashboard cards.

    Never raises — the dashboard/API rely on it always returning JSON.
    Surfaces the selected account snapshot (id, name, balance, canTrade,
    isVisible), the cached-token state, and the per-adapter
    positions/orders read-only status (``not_implemented`` for Topstep in
    this phase) so the UI never has to second-guess what the broker
    exposes.
    """
    try:
        probe = broker.test_connection()
    except Exception as exc:  # pragma: no cover - defensive
        probe = {
            "ok": False,
            "provider": broker.provider,
            "status": "error",
            "not_implemented": False,
            "message": f"test_connection raised: {exc.__class__.__name__}",
        }
    credentials = probe.get("credentials") if isinstance(probe, dict) else None
    creds = credentials if isinstance(credentials, dict) else {}
    selected_account = _account_view(probe.get("selected_account"))
    selected_account_name = (
        selected_account.get("name") if selected_account else None
    )
    balance = selected_account.get("balance") if selected_account else None
    can_trade = selected_account.get("can_trade") if selected_account else None
    is_visible = selected_account.get("is_visible") if selected_account else None

    positions_resp = _safe_get_positions(broker)
    orders_resp = _safe_get_orders(broker)
    positions = positions_resp.get("positions") or []
    orders = orders_resp.get("orders") or []

    return {
        "ok": bool(probe.get("ok")),
        "provider": broker.provider,
        "broker_provider": settings.resolved_provider,
        "active_broker_provider": broker.provider,
        "execution_mode": settings.execution_mode,
        "selected_account_id": settings.resolved_account_id or None,
        "selected_account_name": selected_account_name,
        "selected_account": selected_account,
        "broker_connected": bool(probe.get("ok")),
        "broker_message": probe.get("message", ""),
        "not_implemented": bool(probe.get("not_implemented")),
        "status": probe.get("status", "unknown"),
        "auth_status": probe.get("status", "unknown"),
        "balance": balance,
        "account_balance": balance,
        "can_trade": can_trade,
        "is_visible": is_visible,
        "accounts_count": probe.get("accounts_count"),
        "token_cached": bool(creds.get("token_cached")),
        "token_expires_at": _mask_token_expiry(creds.get("token_expires_at")),
        "positions_status": positions_resp.get("status", "unknown"),
        "positions_message": positions_resp.get("message", ""),
        "positions_count": len(positions),
        "positions_not_implemented": bool(
            positions_resp.get("not_implemented")
        ),
        "orders_status": orders_resp.get("status", "unknown"),
        "orders_message": orders_resp.get("message", ""),
        "orders_count": len(orders),
        "orders_not_implemented": bool(orders_resp.get("not_implemented")),
        "restart_required": settings.resolved_provider != broker.provider,
    }


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
