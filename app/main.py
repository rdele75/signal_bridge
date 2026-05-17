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

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import __version__
from .config import Settings, get_settings
from .dashboard import (
    dashboard_summary,
    journal_view,
    metrics_summary,
    tail_log,
)
from .journal import Journal
from .kill_switch import KillSwitch
from .risk_engine import RiskEngine
from .schemas import StatusResponse, WebhookResponse
from .signal_router import build_broker
from .symbol_map import SymbolMap
from .webhook import WebhookHandler


_HERE = Path(__file__).resolve().parent
TEMPLATES_DIR = _HERE / "templates"
STATIC_DIR = _HERE / "static"


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
    app.state.journal = journal
    app.state.kill_switch = kill_switch
    app.state.risk = risk
    app.state.broker = broker
    app.state.symbol_map = symbol_map
    app.state.handler = handler
    app.state.templates = templates

    def _page_ctx(request: Request) -> dict[str, Any]:
        return {
            "request": request,
            "execution_mode": settings.execution_mode,
            "broker_provider": broker.provider,
            "kill_switch_active": kill_switch.is_active(),
        }

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
        return StatusResponse(
            app_name=settings.app_name,
            execution_mode=settings.execution_mode,
            broker_provider=broker.provider,
            broker=settings.broker,
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

    @app.post("/api/broker/test-connection")
    def api_broker_test() -> JSONResponse:
        result = broker.test_connection()
        status_code = 200 if result.get("ok") else 501
        return JSONResponse(content=result, status_code=status_code)

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
        ctx.update(
            {
                "topstep": {
                    "username": settings.topstep_username,
                    "password": settings.topstep_password,
                    "api_key": settings.topstep_api_key,
                    "account_id": settings.topstep_account_id,
                    "env": settings.topstep_env,
                },
                "tradovate": {
                    "username": settings.tradovate_username,
                    "password": settings.tradovate_password,
                    "app_id": settings.tradovate_app_id,
                    "app_version": settings.tradovate_app_version,
                    "cid": settings.tradovate_cid,
                    "sec": settings.tradovate_sec,
                    "account_id": settings.tradovate_account_id,
                    "env": settings.tradovate_env,
                },
            }
        )
        return templates.TemplateResponse(request, "settings_broker.html", ctx)

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
        }
        return templates.TemplateResponse(request, "settings_risk.html", ctx)

    @app.get("/tradingview", response_class=HTMLResponse)
    def page_tradingview(request: Request) -> HTMLResponse:
        ctx = _page_ctx(request)
        webhook_url = (
            f"http://{settings.app_host}:{settings.app_port}/webhooks/tradingview"
        )
        secret = settings.webhook_secret or ""
        secret_set = bool(secret) and secret != "change_me_to_a_long_random_secret"
        secret_preview = (
            secret[:3] + "…" + secret[-2:] if len(secret) >= 6 else "set"
        )
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

    return app


app = create_app()
