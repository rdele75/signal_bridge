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
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from . import __version__
from .config import PROJECT_ROOT, Settings
from .execution.broker_base import BrokerBase
from .journal import Journal
from .kill_switch import KillSwitch


# ET trading-session windows. Approximations — tweak here when the
# operator wants something more precise (futures globex hours,
# pre-/post-market, etc.).
_SESSIONS_ET = (
    # (label, start, end). Windows wrap across midnight if start > end.
    ("New York", dtime(9, 30), dtime(16, 0)),
    ("London",   dtime(3, 0),  dtime(9, 30)),
    ("Asia",     dtime(18, 0), dtime(3, 0)),
)


def _et_now() -> datetime:
    """Return 'now' in US/Eastern. Falls back to naive offset arithmetic
    if zoneinfo can't load the tzdb (tests/CI without tzdata)."""
    try:
        from zoneinfo import ZoneInfo  # py3.9+
        return datetime.now(ZoneInfo("America/New_York"))
    except Exception:  # pragma: no cover - tzdata fallback
        # Crude fallback: assume EST (UTC-5). Better than crashing.
        return datetime.now(timezone.utc) - timedelta(hours=5)


def _in_window(now_t: dtime, start: dtime, end: dtime) -> bool:
    if start <= end:
        return start <= now_t < end
    # Window wraps across midnight (e.g. Asia 18:00 → 03:00 next day).
    return now_t >= start or now_t < end


def current_trading_session(now_et: Optional[datetime] = None) -> str:
    """Return the futures-style session label for an ET datetime.

    Returns "Asia", "London", "New York", or "Off-hours". Tweak
    ``_SESSIONS_ET`` to adjust windows.
    """
    now_et = now_et or _et_now()
    t = now_et.time()
    for label, start, end in _SESSIONS_ET:
        if _in_window(t, start, end):
            return label
    return "Off-hours"


def current_session_time(now_et: Optional[datetime] = None) -> str:
    """Pretty ET time string for the trading-session card."""
    now_et = now_et or _et_now()
    return now_et.strftime("%H:%M:%S ET")


def win_rate(journal: Journal, *, min_trades: int = 3) -> str:
    """Format the closed-trade win rate as a percentage string, or
    'N/A' if there aren't enough trades yet."""
    closed = journal.closed_trade_stats()
    total = closed.get("total", 0)
    wins = closed.get("wins", 0)
    if total < min_trades or total <= 0:
        return "N/A"
    return f"{(wins / total) * 100:.1f}%"


def total_points_percentage(journal: Journal) -> str:
    """Total points expressed against the gross points traded.

    `closed_trade_stats` only tracks signed PnL points, so we can't get a
    true win-vs-loss ratio without absolute points per trade. Return the
    net points percentage as |total_points| / max(|win_pts|, |loss_pts|)
    style is overkill — instead, show a simple net % vs a configurable
    target of 1 point per trade. If insufficient data, return 'N/A'.
    """
    closed = journal.closed_trade_stats()
    total = closed.get("total", 0)
    if total <= 0:
        return "N/A"
    total_pts = float(closed.get("total_points", 0.0) or 0.0)
    denom = float(total)
    pct = (total_pts / denom) * 100.0
    return f"{pct:+.1f}%"


def profit_series(journal: Journal, *, limit: int = 50) -> list[dict[str, Any]]:
    """Return a chronological list of closed-trade points for a sparkline.

    Each item: {"closed_at": iso, "points": float, "cumulative": float}.
    Empty list when there are no closed trades — the template renders
    an empty-state graph container in that case.
    """
    rows = journal.list_recent_closed_trades(limit=limit)
    # list_recent_closed_trades is newest-first. Reverse for a left-to-right
    # cumulative series.
    rows = list(reversed(rows))
    series: list[dict[str, Any]] = []
    running = 0.0
    for r in rows:
        pts = float(r.get("realized_pnl_points") or 0.0)
        running += pts
        series.append(
            {
                "closed_at": r.get("closed_at"),
                "points": pts,
                "cumulative": running,
                "symbol": r.get("symbol"),
            }
        )
    return series


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
    # Post-collapse: dashboard stat cards reflect ONLY signals that
    # were submitted in Armed state (real Topstep orders). Test fills
    # land in the journal too but stay out of the dashboard summary
    # so the operator's "today" view doesn't mix plumbing tests with
    # production activity. The journal-wide counts remain available
    # on /metrics and /journal.
    accepted_today = journal.count_today(
        decision="accepted", execution_mode="armed"
    )
    rejected_today = journal.count_today(
        decision="rejected", execution_mode="armed"
    )
    last_signal = journal.latest_signal()
    last_rejection = journal.latest_signal(decision="rejected")
    daily_pnl = journal.get_daily_pnl()
    closed = journal.closed_trade_stats()

    pnl_display = (
        f"{daily_pnl:+.2f} pts" if closed["total"] > 0 else "N/A"
    )

    configured_provider = settings.resolved_provider
    broker_status = broker_status_payload(settings=settings, broker=broker)

    # Recent broker orders feed the dashboard "Open Orders" table that
    # replaces the old paper-orders block. Only paper has fills right
    # now — placeholder providers come back with an empty list.
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

    session_now = _et_now()

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
        # Broker-side position count drives the Flatten button's
        # disabled state. Flatten acts on broker positions, not the
        # journal (the two can disagree on Topstep where positions
        # may be opened/closed outside SignalBridge).
        "broker_open_position_count": sum(
            1 for p in broker_positions
            if isinstance(p, dict) and p.get("size")
        ),
        "trades_today": accepted_today,
        "accepted_today": accepted_today,
        "rejected_today": rejected_today,
        "last_signal": _signal_for_display(last_signal),
        "last_rejection": _signal_for_display(last_rejection),
        "daily_pnl": daily_pnl,
        "daily_pnl_display": pnl_display,
        "closed_trade_total": closed["total"],
        "win_rate": win_rate(journal),
        "total_points_percentage": total_points_percentage(journal),
        "trading_session": current_trading_session(session_now),
        "session_time": current_session_time(session_now),
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


def metrics_summary(
    *,
    journal: Journal,
    broker: Optional[BrokerBase] = None,
) -> dict[str, Any]:
    closed = journal.closed_trade_stats()
    series = profit_series(journal, limit=100)
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
        "win_rate": win_rate(journal),
        "total_points_percentage": total_points_percentage(journal),
        "daily_pnl": journal.get_daily_pnl(),
        "profit_series": series,
        "profit_chart": _profit_chart(series),
        "past_orders": past_orders_summary(broker=broker, journal=journal),
    }


def past_orders_summary(
    *,
    broker: Optional[BrokerBase],
    journal: Journal,
    limit: int = 25,
) -> dict[str, Any]:
    """Return a normalized "past orders" view for the metrics page.

    Tries the active broker's ``get_orders()`` first. Falls back to the
    journal when the broker has no order history (or returns
    ``not_implemented``). The returned shape is always safe to render:

      {
        "rows": [...],
        "source": "broker_paper" | "broker_topstep" | "journal",
        "status": "ok" | "empty" | "broker_unavailable" | "fallback_journal",
        "message": str,
        "provider": str,
        "broker_status_label": str | None,
      }

    No fabrication: when neither the broker nor the journal has rows,
    the empty list is returned with an explanatory message.
    """
    provider = getattr(broker, "provider", "") or ""

    broker_resp: dict[str, Any] = {}
    broker_rows: list[dict[str, Any]] = []
    broker_ok = False
    broker_not_implemented = False
    if broker is not None:
        try:
            broker_resp = broker.get_orders() or {}
        except Exception:  # pragma: no cover - defensive
            broker_resp = {}
        if isinstance(broker_resp, dict):
            broker_ok = bool(broker_resp.get("ok"))
            broker_not_implemented = bool(
                broker_resp.get("not_implemented")
            )
            raw_rows = broker_resp.get("orders") or []
            if isinstance(raw_rows, list):
                broker_rows = [
                    _normalize_broker_order_row(r, provider=provider)
                    for r in raw_rows
                    if isinstance(r, dict)
                ]

    broker_status_label = (
        str(broker_resp.get("status")) if broker_resp.get("status") else None
    )

    if broker_ok and broker_rows:
        rows = broker_rows[:limit]
        return {
            "rows": rows,
            "source": f"broker_{provider}" if provider else "broker",
            "status": "ok",
            "message": f"{len(rows)} order(s) from {provider or 'broker'}",
            "provider": provider,
            "broker_status_label": broker_status_label,
        }

    if broker_ok and not broker_rows:
        message = "No broker orders yet."
        return {
            "rows": [],
            "source": f"broker_{provider}" if provider else "broker",
            "status": "empty",
            "message": message,
            "provider": provider,
            "broker_status_label": broker_status_label,
        }

    # Broker is unavailable (not_implemented, auth_failed, network, etc.) —
    # fall back to the journal. Never fabricate.
    journal_rows = _journal_past_orders(journal=journal, limit=limit)

    if provider == "topstep" and broker_not_implemented and not journal_rows:
        return {
            "rows": [],
            "source": "journal",
            "status": "topstep_not_available",
            "message": "Topstep order history is not available yet.",
            "provider": provider,
            "broker_status_label": broker_status_label,
        }

    if not journal_rows:
        return {
            "rows": [],
            "source": "journal",
            "status": "empty",
            "message": "No past orders yet.",
            "provider": provider,
            "broker_status_label": broker_status_label,
        }

    return {
        "rows": journal_rows[:limit],
        "source": "journal",
        "status": "fallback_journal",
        "message": (
            f"{len(journal_rows[:limit])} signal(s) from local journal"
        ),
        "provider": provider,
        "broker_status_label": broker_status_label,
    }


def _normalize_broker_order_row(
    row: dict[str, Any], *, provider: str
) -> dict[str, Any]:
    """Project a broker ``get_orders`` row into the shape the metrics
    template renders.

    Both the paper adapter (signal-shaped rows) and the Topstep adapter
    (ProjectX raw order rows) feed through here so the template stays
    dumb. Unknown fields are passed through as ``None`` rather than
    fabricated.
    """
    time_value = (
        row.get("received_at")
        or row.get("creationTimestamp")
        or row.get("updateTimestamp")
        or row.get("time")
    )
    symbol = (
        row.get("symbol")
        or row.get("broker_symbol")
        or row.get("contractId")
    )
    action = row.get("action")
    if action is None and "side" in row:
        side_raw = row.get("side")
        if side_raw == 0:
            action = "BUY"
        elif side_raw == 1:
            action = "SELL"
        else:
            action = str(side_raw) if side_raw is not None else None
    size = row.get("contracts")
    if size is None:
        size = row.get("size")
    status = (
        row.get("status")
        or row.get("decision")
        or row.get("rejection_reason")
        or row.get("state")
    )
    order_id = (
        row.get("order_id") or row.get("orderId") or row.get("id")
    )
    source = row.get("source") or row.get("customTag")
    strategy = row.get("strategy")
    source_label = source or strategy or row.get("execution_mode")
    return {
        "time": time_value,
        "broker": row.get("broker_provider") or provider or None,
        "symbol": symbol,
        "action": action,
        "size": size,
        "status": status,
        "order_id": str(order_id) if order_id is not None else None,
        "source": source_label,
        "strategy": strategy,
    }


def _journal_past_orders(
    *, journal: Journal, limit: int
) -> list[dict[str, Any]]:
    """Fall-back order rows derived from the signal journal.

    Only keeps signals that actually represent an order attempt —
    either they have a real ``order_id``/``broker_order_id`` or
    ``execution_result`` carries a dry-run payload. This stops the
    table from filling up with malformed-payload rejection rows.
    """
    rows = journal.list_recent_signals(limit=max(limit * 4, limit))
    out: list[dict[str, Any]] = []
    for r in rows:
        if not r.get("order_id") and r.get("decision") != "accepted":
            # Pre-risk rejections without an order id aren't useful here.
            if not r.get("symbol") or not r.get("action"):
                continue
        status = r.get("decision") or "unknown"
        if r.get("rejection_reason"):
            status = f"{status}:{r.get('rejection_reason')}"
        out.append(
            {
                "time": r.get("received_at"),
                "broker": r.get("broker_provider"),
                "symbol": r.get("symbol") or r.get("broker_symbol"),
                "action": r.get("action"),
                "size": r.get("contracts"),
                "status": status,
                "order_id": (
                    str(r["order_id"]) if r.get("order_id") else None
                ),
                "source": r.get("source"),
                "strategy": r.get("strategy"),
            }
        )
        if len(out) >= limit:
            break
    return out


def _profit_chart(series: list[dict[str, Any]]) -> dict[str, Any]:
    """Pre-compute SVG points/min/max for the metrics page profit graph.

    Returns ``{"empty": True}`` when no data — the template renders the
    empty-state box. Otherwise returns SVG-ready geometry so the
    template stays dumb."""
    if not series:
        return {"empty": True, "points": "", "min": 0.0, "max": 0.0}
    cumul = [float(p.get("cumulative") or 0.0) for p in series]
    lo = min(cumul + [0.0])
    hi = max(cumul + [0.0])
    width = 600
    height = 160
    pad_x = 4
    pad_y = 8
    span = hi - lo if hi != lo else 1.0
    n = max(len(cumul) - 1, 1)
    coords: list[str] = []
    for i, v in enumerate(cumul):
        x = pad_x + (i / n) * (width - 2 * pad_x)
        # SVG y grows downward — invert.
        y = pad_y + (1 - (v - lo) / span) * (height - 2 * pad_y)
        coords.append(f"{x:.1f},{y:.1f}")
    return {
        "empty": False,
        "points": " ".join(coords),
        "width": width,
        "height": height,
        "min": lo,
        "max": hi,
        "final": cumul[-1],
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
        "is_funded": _classify_funded(account),
    }


# Substrings (case-insensitive) that, if present in the account name,
# indicate a non-funded account. ProjectX doesn't expose a structured
# "funded" flag on /api/Account/search, so a heuristic on the name is
# the most reliable signal available today. The dashboard surfaces an
# explicit "unknown" badge when we can't classify, so the operator
# always sees confirmation of what they're trading even when the
# heuristic abstains.
_NON_FUNDED_NAME_HINTS: tuple[str, ...] = (
    "PRACTICE",
    "EVAL",
    "TRIAL",
    "DEMO",
    "SIM",
    "COMBINE",
    "EXPRESS",
)


def _classify_funded(account: dict[str, Any]) -> Optional[bool]:
    """Best-effort 'is this a funded account?' classification.

    Returns ``True`` when the account name clearly does NOT match any
    practice/eval/trial keyword; ``False`` when it does; ``None`` when
    we have nothing to go on (no name, no usable hints). Phase 4 polish
    should swap this for a real ProjectX field if one becomes
    available.
    """
    name = account.get("name")
    if not isinstance(name, str) or not name.strip():
        return None
    upper = name.upper()
    for hint in _NON_FUNDED_NAME_HINTS:
        if hint in upper:
            return False
    return True


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
    selected_account_is_funded = (
        selected_account.get("is_funded") if selected_account else None
    )

    positions_resp = _safe_get_positions(broker)
    orders_resp = _safe_get_orders(broker)
    positions = positions_resp.get("positions") or []
    orders = orders_resp.get("orders") or []
    open_orders_count = len(orders)

    return {
        "ok": bool(probe.get("ok")),
        "provider": broker.provider,
        "broker_provider": settings.resolved_provider,
        "active_broker_provider": broker.provider,
        "execution_mode": settings.execution_mode,
        "selected_account_id": settings.resolved_account_id or None,
        "selected_account_name": selected_account_name,
        "selected_account": selected_account,
        "selected_account_is_funded": selected_account_is_funded,
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
        "orders_count": open_orders_count,
        "open_orders_count": open_orders_count,
        "orders_not_implemented": bool(orders_resp.get("not_implemented")),
        "restart_required": settings.resolved_provider != broker.provider,
        # Execution state and the structural caps. The dashboard JS
        # consumes these to render the execution card and decide which
        # buttons to expose.
        "execution_state": (settings.execution_mode or "off").lower(),
        "allowed_symbols_armed": list(settings.allowed_symbols_armed),
        # Risk sizing knobs so the dashboard/JS can render which mode
        # is active and what the hard cap is.
        "strategy_managed_risk": settings.strategy_managed_risk,
        "fixed_contracts_per_trade": settings.fixed_contracts_per_trade,
        "max_contracts_per_trade": settings.max_contracts_per_trade,
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
