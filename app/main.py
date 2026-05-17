"""FastAPI entry point for SignalBridge."""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from typing import Any

from fastapi import FastAPI, Request

from . import __version__
from .config import Settings, get_settings
from .journal import Journal
from .kill_switch import KillSwitch
from .risk_engine import RiskEngine
from .schemas import StatusResponse, WebhookResponse
from .signal_router import build_broker
from .webhook import WebhookHandler


def _configure_logging(settings: Settings) -> None:
    settings.log_abs_path.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger("signalbridge")
    root.setLevel(settings.log_level)
    # Avoid duplicate handlers under uvicorn reload.
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

    # Also surface to console so `uvicorn` shows our logs.
    sh = logging.StreamHandler()
    sh._signalbridge = True  # type: ignore[attr-defined]
    sh.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
    root.addHandler(sh)


def create_app() -> FastAPI:
    settings = get_settings()
    _configure_logging(settings)

    log = logging.getLogger("signalbridge")
    log.info("starting SignalBridge v%s mode=%s broker=%s",
             __version__, settings.execution_mode, settings.broker)

    journal = Journal(settings.database_abs_path)
    kill_switch = KillSwitch(
        settings.database_abs_path.parent / "kill_switch.active",
        enabled=settings.enable_kill_switch,
    )
    risk = RiskEngine(settings=settings, journal=journal, kill_switch=kill_switch)
    broker = build_broker(settings, journal)
    handler = WebhookHandler(
        settings=settings, journal=journal, risk=risk, broker=broker
    )

    app = FastAPI(title=settings.app_name, version=__version__)

    # Stash for tests / introspection.
    app.state.settings = settings
    app.state.journal = journal
    app.state.kill_switch = kill_switch
    app.state.risk = risk
    app.state.broker = broker
    app.state.handler = handler

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "app": settings.app_name,
            "version": __version__,
        }

    @app.get("/status", response_model=StatusResponse)
    def status() -> StatusResponse:
        return StatusResponse(
            app_name=settings.app_name,
            execution_mode=settings.execution_mode,
            broker=settings.broker,
            allowed_symbols=list(settings.allowed_symbols),
            kill_switch_active=kill_switch.is_active(),
            open_positions=journal.list_open_positions(),
            database_path=str(settings.database_abs_path),
        )

    @app.post("/webhooks/tradingview", response_model=WebhookResponse)
    async def tradingview_webhook(request: Request) -> WebhookResponse:
        try:
            payload = await request.json()
        except Exception:
            payload = None
        return handler.handle(payload)

    return app


app = create_app()
