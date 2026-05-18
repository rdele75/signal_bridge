"""FastAPI entry point for SignalBridge.

Wires together:
  * the webhook endpoint TradingView posts to
  * REST APIs for the dashboard JS
  * server-rendered HTML pages (Jinja2)
  * static assets (CSS)
"""
from __future__ import annotations

import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import __version__
from .config import Settings, get_settings
from .dashboard import (
    broker_status_payload,
    dashboard_summary,
    journal_view,
    metrics_summary,
    system_summary,
    tail_log,
)
from .journal import Journal
from .kill_switch import KillSwitch
from .risk_engine import RiskEngine
from .schemas import StatusResponse, WebhookResponse
from .settings_store import (
    SettingsStore,
    SettingsValidationError,
    generate_secret,
    webhook_secret_preview,
)
from .signal_router import build_broker
from .symbol_map import SymbolMap
from .webhook import WebhookHandler


_HERE = Path(__file__).resolve().parent
TEMPLATES_DIR = _HERE / "templates"
STATIC_DIR = _HERE / "static"


def _mask_identifier(value: str) -> str:
    """Return a masked version of a username-like identifier.

    Used by the broker settings page so we don't echo full usernames
    back to the UI. Empty/short values come back empty.
    """
    if not value:
        return ""
    text = str(value)
    if len(text) <= 2:
        return "•" * len(text)
    if len(text) <= 4:
        return text[0] + "•" * (len(text) - 1)
    return f"{text[:2]}{'•' * (len(text) - 4)}{text[-2:]}"


def _configure_logging(settings: Settings) -> None:
    settings.log_abs_path.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger("signalbridge")
    root.setLevel(settings.log_level)
    if any(getattr(h, "_signalbridge", False) for h in root.handlers):
        return
    fh = RotatingFileHandler(
        settings.log_abs_path, maxBytes=5_000_000, backupCount=3
    )
    fh._signalbridge = True  # type: ignore[attr-defined]
    fh.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    root.addHandler(fh)

    sh = logging.StreamHandler()
    sh._signalbridge = True  # type: ignore[attr-defined]
    sh.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
    root.addHandler(sh)


def create_app() -> FastAPI:
    settings = get_settings()
    _configure_logging(settings)

    log = logging.getLogger("signalbridge")
    log.info(
        "starting SignalBridge v%s mode=%s broker=%s",
        __version__,
        settings.execution_mode,
        settings.resolved_provider,
    )

    journal = Journal(settings.database_abs_path)
    settings_store = SettingsStore(settings.database_abs_path)
    settings_store.initialize_settings_from_env(settings)
    kill_switch = KillSwitch(
        settings.database_abs_path.parent / "kill_switch.active",
        enabled=settings.enable_kill_switch,
    )
    risk = RiskEngine(settings=settings, journal=journal, kill_switch=kill_switch)
    broker = build_broker(settings, journal)
    symbol_map = SymbolMap(settings.symbols_map_abs_path)
    handler = WebhookHandler(
        settings=settings,
        journal=journal,
        risk=risk,
        broker=broker,
        symbol_map=symbol_map,
    )

    app = FastAPI(title=settings.app_name, version=__version__)
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    app.state.settings = settings
    app.state.settings_store = settings_store
    app.state.journal = journal
    app.state.kill_switch = kill_switch
    app.state.risk = risk
    app.state.broker = broker
    app.state.symbol_map = symbol_map
    app.state.handler = handler
    app.state.templates = templates

    def _page_ctx(request: Request) -> dict[str, Any]:
        flash = request.query_params.get("flash")
        flash_kind = request.query_params.get("flash_kind", "info")
        return {
            "request": request,
            "app_name": settings.app_name,
            "app_version": __version__,
            "execution_mode": settings.execution_mode,
            "broker_provider": settings.resolved_provider,
            "active_broker_provider": broker.provider,
            "kill_switch_active": kill_switch.is_active(),
            "flash": flash,
            "flash_kind": flash_kind if flash_kind in {"ok", "error", "info"} else "info",
        }

    def _flash_redirect(path: str, message: str, kind: str = "ok") -> RedirectResponse:
        from urllib.parse import urlencode
        qs = urlencode({"flash": message, "flash_kind": kind})
        return RedirectResponse(url=f"{path}?{qs}", status_code=303)

    # ------------------------------------------------------------------
    # JSON endpoints
    # ------------------------------------------------------------------

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "app": settings.app_name,
            "version": __version__,
        }

    def _status_payload() -> StatusResponse:
        broker_snapshot = broker_status_payload(settings=settings, broker=broker)
        return StatusResponse(
            app_name=settings.app_name,
            execution_mode=settings.execution_mode,
            broker_provider=settings.resolved_provider,
            broker=settings.broker,
            selected_account_id=broker_snapshot["selected_account_id"],
            broker_connected=broker_snapshot["broker_connected"],
            broker_message=broker_snapshot["broker_message"],
            allowed_symbols=list(settings.allowed_symbols),
            kill_switch_active=kill_switch.is_active(),
            open_positions=journal.list_open_positions(),
            database_path=str(settings.database_abs_path),
        )

    @app.get("/status", response_model=StatusResponse)
    def status() -> StatusResponse:
        return _status_payload()

    @app.get("/api/status", response_model=StatusResponse)
    def api_status() -> StatusResponse:
        return _status_payload()

    @app.get("/api/metrics")
    def api_metrics() -> dict[str, Any]:
        return metrics_summary(journal=journal)

    @app.get("/api/journal/recent")
    def api_journal_recent(limit: int = 50) -> dict[str, Any]:
        limit = max(1, min(int(limit), 500))
        return {
            "signals": journal.list_recent_signals(limit=limit),
            "closed_trades": journal.list_recent_closed_trades(limit=limit),
        }

    @app.get("/api/positions")
    def api_positions() -> dict[str, Any]:
        return {"open_positions": journal.list_open_positions()}

    @app.post("/api/kill-switch/enable")
    def api_kill_switch_enable() -> dict[str, Any]:
        kill_switch.activate("manual via dashboard")
        return {"ok": True, "kill_switch_active": kill_switch.is_active()}

    @app.post("/api/kill-switch/disable")
    def api_kill_switch_disable() -> dict[str, Any]:
        kill_switch.deactivate()
        return {"ok": True, "kill_switch_active": kill_switch.is_active()}

    def _safe_broker_call(method_name: str, *args, **kwargs) -> dict[str, Any]:
        """Wrap a broker method so it always returns a JSON-friendly dict."""
        fn = getattr(broker, method_name, None)
        if fn is None:
            return {
                "ok": False,
                "provider": broker.provider,
                "not_implemented": True,
                "status": "not_implemented",
                "message": f"{broker.provider} has no {method_name}()",
            }
        try:
            result = fn(*args, **kwargs)
        except NotImplementedError as exc:
            return {
                "ok": False,
                "provider": broker.provider,
                "not_implemented": True,
                "status": "not_implemented",
                "message": str(exc) or f"{broker.provider} {method_name} not implemented",
            }
        except Exception as exc:  # pragma: no cover - defensive
            return {
                "ok": False,
                "provider": broker.provider,
                "not_implemented": False,
                "status": "error",
                "message": f"{method_name} raised: {exc.__class__.__name__}",
            }
        return result if isinstance(result, dict) else {"ok": True, "result": result}

    @app.get("/api/broker/status")
    def api_broker_status() -> dict[str, Any]:
        return broker_status_payload(settings=settings, broker=broker)

    @app.post("/api/broker/test-connection")
    def api_broker_test() -> JSONResponse:
        result = _safe_broker_call("test_connection")
        status_code = 200 if result.get("ok") else 501
        return JSONResponse(content=result, status_code=status_code)

    @app.get("/api/broker/accounts")
    def api_broker_accounts() -> dict[str, Any]:
        return _safe_broker_call("get_accounts")

    @app.get("/api/broker/positions")
    def api_broker_positions() -> dict[str, Any]:
        return _safe_broker_call("get_positions")

    @app.get("/api/broker/orders")
    def api_broker_orders() -> dict[str, Any]:
        return _safe_broker_call("get_orders")

    @app.get("/api/system")
    def api_system() -> dict[str, Any]:
        return system_summary(
            settings=settings, broker=broker, kill_switch=kill_switch
        )

    @app.post("/webhooks/tradingview", response_model=WebhookResponse)
    async def tradingview_webhook(request: Request) -> WebhookResponse:
        try:
            payload = await request.json()
        except Exception:
            payload = None
        return handler.handle(payload)

    # ------------------------------------------------------------------
    # HTML pages
    # ------------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def page_dashboard(request: Request) -> HTMLResponse:
        ctx = _page_ctx(request)
        ctx.update(
            dashboard_summary(
                settings=settings,
                journal=journal,
                kill_switch=kill_switch,
                broker=broker,
            )
        )
        return templates.TemplateResponse(request, "dashboard.html", ctx)

    @app.get("/settings/broker", response_class=HTMLResponse)
    def page_settings_broker(request: Request) -> HTMLResponse:
        ctx = _page_ctx(request)
        configured = settings.resolved_provider
        broker_snapshot = broker_status_payload(settings=settings, broker=broker)
        accounts = _safe_broker_call("get_accounts")
        ctx.update(
            {
                "topstep": {
                    "username_set": bool(settings.topstep_username),
                    "username_preview": _mask_identifier(settings.topstep_username),
                    "password_set": bool(settings.topstep_password),
                    "api_key_set": bool(settings.topstep_api_key),
                    "account_id": settings.topstep_account_id,
                    "env": settings.topstep_env,
                },
                "tradovate": {
                    "username_set": bool(settings.tradovate_username),
                    "username_preview": _mask_identifier(settings.tradovate_username),
                    "password_set": bool(settings.tradovate_password),
                    "app_id_set": bool(settings.tradovate_app_id),
                    "app_version": settings.tradovate_app_version,
                    "cid_set": bool(settings.tradovate_cid),
                    "sec_set": bool(settings.tradovate_sec),
                    "account_id": settings.tradovate_account_id,
                    "env": settings.tradovate_env,
                },
                "provider_options": ["paper", "topstep", "tradovate"],
                "execution_mode_options": ["paper", "demo"],
                "configured_provider": configured,
                "configured_execution_mode": settings.execution_mode,
                "selected_account_id": settings.resolved_account_id,
                "selected_account_id_raw": settings.selected_account_id,
                "restart_required": configured != broker.provider,
                "broker_status": broker_snapshot,
                "broker_accounts": accounts,
            }
        )
        return templates.TemplateResponse(request, "settings_broker.html", ctx)

    @app.post("/settings/broker")
    def post_settings_broker(
        broker_provider: str = Form(...),
        execution_mode: str = Form(...),
        selected_account_id: str = Form(""),
    ):
        try:
            new_provider = settings_store.update_typed(
                "BROKER_PROVIDER", broker_provider
            )
            new_mode = settings_store.update_typed(
                "EXECUTION_MODE", execution_mode
            )
            new_account = settings_store.update_typed(
                "SELECTED_ACCOUNT_ID", selected_account_id
            )
        except SettingsValidationError as exc:
            return _flash_redirect("/settings/broker", str(exc), kind="error")

        settings_store.apply_to_settings(
            settings, "BROKER_PROVIDER", new_provider
        )
        settings_store.apply_to_settings(
            settings, "EXECUTION_MODE", new_mode
        )
        settings_store.apply_to_settings(
            settings, "SELECTED_ACCOUNT_ID", new_account
        )
        msg = "Broker settings saved."
        if new_provider != broker.provider:
            msg += " Restart required to switch the active adapter."
        return _flash_redirect("/settings/broker", msg, kind="ok")

    @app.get("/settings/risk", response_class=HTMLResponse)
    def page_settings_risk(request: Request) -> HTMLResponse:
        ctx = _page_ctx(request)
        ctx["risk"] = {
            "max_contracts_per_trade": settings.max_contracts_per_trade,
            "max_open_positions": settings.max_open_positions,
            "max_daily_loss": settings.max_daily_loss,
            "duplicate_order_cooldown_seconds": settings.duplicate_order_cooldown_seconds,
            "enable_longs": settings.enable_longs,
            "enable_shorts": settings.enable_shorts,
            "enable_kill_switch": settings.enable_kill_switch,
            "allowed_symbols": list(settings.allowed_symbols),
            "allowed_symbols_csv": ", ".join(settings.allowed_symbols),
            "enable_timeframe_lock": settings.enable_timeframe_lock,
            "allowed_timeframes": list(settings.allowed_timeframes),
            "allowed_timeframes_csv": ",".join(settings.allowed_timeframes),
        }
        return templates.TemplateResponse(request, "settings_risk.html", ctx)

    @app.post("/settings/risk")
    def post_settings_risk(
        allowed_symbols: str = Form(""),
        max_contracts_per_trade: str = Form(...),
        max_daily_loss: str = Form(...),
        max_open_positions: str = Form(...),
        duplicate_order_cooldown_seconds: str = Form(...),
        enable_longs: str = Form("false"),
        enable_shorts: str = Form("false"),
        enable_timeframe_lock: str = Form("false"),
        allowed_timeframes: str = Form(""),
    ):
        updates = {
            "ALLOWED_SYMBOLS": allowed_symbols,
            "MAX_CONTRACTS_PER_TRADE": max_contracts_per_trade,
            "MAX_DAILY_LOSS": max_daily_loss,
            "MAX_OPEN_POSITIONS": max_open_positions,
            "DUPLICATE_ORDER_COOLDOWN_SECONDS": duplicate_order_cooldown_seconds,
            "ENABLE_LONGS": enable_longs,
            "ENABLE_SHORTS": enable_shorts,
            "ENABLE_TIMEFRAME_LOCK": enable_timeframe_lock,
            "ALLOWED_TIMEFRAMES": allowed_timeframes,
        }
        try:
            coerced = {
                key: settings_store.update_typed(key, value)
                for key, value in updates.items()
            }
        except SettingsValidationError as exc:
            return _flash_redirect("/settings/risk", str(exc), kind="error")

        for key, value in coerced.items():
            settings_store.apply_to_settings(settings, key, value)
        return _flash_redirect(
            "/settings/risk", "Risk settings saved.", kind="ok"
        )

    @app.get("/tradingview", response_class=HTMLResponse)
    def page_tradingview(request: Request) -> HTMLResponse:
        ctx = _page_ctx(request)
        webhook_url = (
            f"http://{settings.app_host}:{settings.app_port}/webhooks/tradingview"
        )
        secret = settings.webhook_secret or ""
        secret_set = bool(secret) and secret != "change_me_to_a_long_random_secret"
        secret_preview = webhook_secret_preview(secret)
        alert_template = json.dumps(
            {
                "secret": "<your TRADINGVIEW_WEBHOOK_SECRET>",
                "source": "tradingview",
                "strategy": "orb_200ema_confluence",
                "symbol": "{{ticker}}",
                "exchange": "{{exchange}}",
                "action": "{{strategy.order.action}}",
                "contracts": "{{strategy.order.contracts}}",
                "price": "{{strategy.order.price}}",
                "position_size": "{{strategy.position_size}}",
                "market_position": "{{strategy.market_position}}",
                "order_id": "{{strategy.order.id}}",
                "comment": "{{strategy.order.comment}}",
                "timeframe": "{{interval}}",
                "bar_time": "{{time}}",
                "fire_time": "{{timenow}}",
            },
            indent=2,
        )
        ctx.update(
            {
                "webhook_url": webhook_url,
                "host": settings.app_host,
                "port": settings.app_port,
                "secret_set": secret_set,
                "secret_preview": secret_preview,
                "alert_template": alert_template,
                "allowed_symbols": list(settings.allowed_symbols),
            }
        )
        return templates.TemplateResponse(request, "tradingview.html", ctx)

    @app.post("/tradingview/secret")
    def post_tradingview_secret(webhook_secret: str = Form(...)):
        try:
            new_secret = settings_store.update_typed(
                "TRADINGVIEW_WEBHOOK_SECRET", webhook_secret
            )
        except SettingsValidationError as exc:
            return _flash_redirect("/tradingview", str(exc), kind="error")
        settings_store.apply_to_settings(
            settings, "TRADINGVIEW_WEBHOOK_SECRET", new_secret
        )
        return _flash_redirect(
            "/tradingview", "Webhook secret updated.", kind="ok"
        )

    @app.post("/tradingview/secret/regenerate")
    def post_tradingview_secret_regenerate():
        new_secret = generate_secret()
        settings_store.set_setting("TRADINGVIEW_WEBHOOK_SECRET", new_secret)
        settings_store.apply_to_settings(
            settings, "TRADINGVIEW_WEBHOOK_SECRET", new_secret
        )
        return _flash_redirect(
            "/tradingview",
            "Generated a new webhook secret. Update your TradingView alerts.",
            kind="ok",
        )

    @app.get("/journal", response_class=HTMLResponse)
    def page_journal(request: Request) -> HTMLResponse:
        ctx = _page_ctx(request)
        ctx.update(journal_view(journal=journal, limit=100))
        return templates.TemplateResponse(request, "journal.html", ctx)

    @app.get("/metrics", response_class=HTMLResponse)
    def page_metrics(request: Request) -> HTMLResponse:
        ctx = _page_ctx(request)
        ctx["m"] = metrics_summary(journal=journal)
        return templates.TemplateResponse(request, "metrics.html", ctx)

    @app.get("/logs", response_class=HTMLResponse)
    def page_logs(request: Request) -> HTMLResponse:
        ctx = _page_ctx(request)
        ctx["log_path"] = str(settings.log_abs_path)
        ctx["lines"] = tail_log(settings.log_abs_path, lines=300)
        return templates.TemplateResponse(request, "logs.html", ctx)

    @app.get("/system", response_class=HTMLResponse)
    def page_system(request: Request) -> HTMLResponse:
        ctx = _page_ctx(request)
        ctx["sys"] = system_summary(
            settings=settings, broker=broker, kill_switch=kill_switch
        )
        return templates.TemplateResponse(request, "system.html", ctx)

    return app


app = create_app()
