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

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import __version__
from .auth import (
    LoginRequired,
    check_credentials,
    login as auth_login,
    logout as auth_logout,
    require_admin_api,
    require_admin_page,
    safe_next_path,
    warn_if_default_secrets,
)
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
from .execution.topstep import TopstepBroker
from .signal_router import _topstep_token_sink, build_broker
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
    broker = build_broker(settings, journal, settings_store=settings_store)
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

    warn_if_default_secrets(settings, log)

    if settings.admin_auth_enabled:
        app.add_middleware(
            SessionMiddleware,
            secret_key=settings.session_secret or "signalbridge-fallback-secret",
            session_cookie="signalbridge_session",
            same_site="lax",
            https_only=False,
        )

    @app.exception_handler(LoginRequired)
    async def _login_required_handler(request: Request, exc: LoginRequired):
        from urllib.parse import urlencode
        qs = urlencode({"next": exc.next_path}) if exc.next_path else ""
        target = f"/login?{qs}" if qs else "/login"
        return RedirectResponse(url=target, status_code=303)

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
            "auth_enabled": settings.admin_auth_enabled,
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

    @app.get(
        "/status",
        response_model=StatusResponse,
        dependencies=[Depends(require_admin_api)],
    )
    def status() -> StatusResponse:
        return _status_payload()

    @app.get(
        "/api/status",
        response_model=StatusResponse,
        dependencies=[Depends(require_admin_api)],
    )
    def api_status() -> StatusResponse:
        return _status_payload()

    @app.get("/api/metrics", dependencies=[Depends(require_admin_api)])
    def api_metrics() -> dict[str, Any]:
        return metrics_summary(journal=journal)

    @app.get(
        "/api/journal/recent", dependencies=[Depends(require_admin_api)]
    )
    def api_journal_recent(limit: int = 50) -> dict[str, Any]:
        limit = max(1, min(int(limit), 500))
        return {
            "signals": journal.list_recent_signals(limit=limit),
            "closed_trades": journal.list_recent_closed_trades(limit=limit),
        }

    @app.get("/api/positions", dependencies=[Depends(require_admin_api)])
    def api_positions() -> dict[str, Any]:
        return {"open_positions": journal.list_open_positions()}

    @app.post(
        "/api/kill-switch/enable", dependencies=[Depends(require_admin_api)]
    )
    def api_kill_switch_enable() -> dict[str, Any]:
        kill_switch.activate("manual via dashboard")
        return {"ok": True, "kill_switch_active": kill_switch.is_active()}

    @app.post(
        "/api/kill-switch/disable",
        dependencies=[Depends(require_admin_api)],
    )
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

    def _test_connection_status_code(result: dict[str, Any]) -> int:
        """Pick the HTTP status code for /api/broker/test-connection.

        Documented adapter states (ok / missing_credentials /
        scaffolded_not_connected / not_implemented) return 200 — the
        envelope itself carries the detail. Anything else is treated as
        an internal error.
        """
        if result.get("ok"):
            return 200
        status_value = (result.get("status") or "").lower()
        documented = {
            "ok",
            "missing_credentials",
            "scaffolded_not_connected",
            "not_implemented",
        }
        if status_value in documented or result.get("not_implemented"):
            return 200
        return 500

    @app.get(
        "/api/broker/status", dependencies=[Depends(require_admin_api)]
    )
    def api_broker_status() -> dict[str, Any]:
        return broker_status_payload(settings=settings, broker=broker)

    @app.post(
        "/api/broker/test-connection",
        dependencies=[Depends(require_admin_api)],
    )
    def api_broker_test() -> JSONResponse:
        result = _safe_broker_call("test_connection")
        status_code = _test_connection_status_code(result)
        return JSONResponse(content=result, status_code=status_code)

    @app.get(
        "/api/broker/accounts", dependencies=[Depends(require_admin_api)]
    )
    def api_broker_accounts() -> dict[str, Any]:
        return _safe_broker_call("get_accounts")

    def _topstep_adapter_for_admin() -> TopstepBroker:
        """Topstep adapter for admin endpoints.

        Re-uses the live broker when it's already a TopstepBroker (so its
        cached token sticks around). Otherwise it builds a transient
        TopstepBroker from current settings so the admin endpoints work
        before the operator has flipped BROKER_PROVIDER + restarted.
        """
        if isinstance(broker, TopstepBroker):
            return broker
        return TopstepBroker(
            username=settings.topstep_username,
            password=settings.topstep_password,
            api_key=settings.topstep_api_key,
            account_id=(
                settings.topstep_account_id or settings.selected_account_id
            ),
            env=settings.topstep_env,
            base_url=settings.topstep_base_url,
            ws_url=settings.topstep_ws_url,
            token=settings.topstep_token,
            token_expires_at=settings.topstep_token_expires_at,
            token_sink=_topstep_token_sink(settings, settings_store),
        )

    def _safe_topstep_call(method_name: str, *args, **kwargs) -> dict[str, Any]:
        topstep = _topstep_adapter_for_admin()
        fn = getattr(topstep, method_name)
        try:
            result = fn(*args, **kwargs)
        except NotImplementedError as exc:
            return {
                "ok": False,
                "provider": "topstep",
                "not_implemented": True,
                "status": "not_implemented",
                "message": str(exc) or f"topstep {method_name} not implemented",
            }
        except Exception as exc:  # pragma: no cover - defensive
            return {
                "ok": False,
                "provider": "topstep",
                "status": "error",
                "message": f"{method_name} raised: {exc.__class__.__name__}",
            }
        return result if isinstance(result, dict) else {"ok": True, "result": result}

    @app.post(
        "/api/topstep/authenticate",
        dependencies=[Depends(require_admin_api)],
    )
    def api_topstep_authenticate() -> JSONResponse:
        result = _safe_topstep_call("authenticate")
        return JSONResponse(content=result, status_code=200)

    @app.get(
        "/api/topstep/accounts", dependencies=[Depends(require_admin_api)]
    )
    def api_topstep_accounts_get() -> JSONResponse:
        result = _safe_topstep_call("get_accounts")
        return JSONResponse(content=result, status_code=200)

    @app.post(
        "/api/topstep/accounts", dependencies=[Depends(require_admin_api)]
    )
    def api_topstep_accounts_post() -> JSONResponse:
        result = _safe_topstep_call("get_accounts")
        return JSONResponse(content=result, status_code=200)

    @app.post(
        "/api/topstep/select-account",
        dependencies=[Depends(require_admin_api)],
    )
    def api_topstep_select_account(
        account_id: str = Form(...),
    ) -> JSONResponse:
        try:
            topstep_acct = settings_store.update_typed(
                "TOPSTEP_ACCOUNT_ID", account_id
            )
            selected_acct = settings_store.update_typed(
                "SELECTED_ACCOUNT_ID", account_id
            )
        except SettingsValidationError as exc:
            return JSONResponse(
                status_code=400,
                content={"ok": False, "message": str(exc)},
            )
        settings_store.apply_to_settings(
            settings, "TOPSTEP_ACCOUNT_ID", topstep_acct
        )
        settings_store.apply_to_settings(
            settings, "SELECTED_ACCOUNT_ID", selected_acct
        )
        # Mirror onto the active broker so the next call reflects the
        # new selection without a restart.
        if isinstance(broker, TopstepBroker):
            broker.account_id = topstep_acct
        return JSONResponse(
            status_code=200,
            content={
                "ok": True,
                "provider": "topstep",
                "selected_account_id": selected_acct,
                "topstep_account_id": topstep_acct,
                "message": (
                    f"Saved {topstep_acct or '(empty)'} as the selected "
                    "Topstep account."
                ),
            },
        )

    @app.get(
        "/api/broker/positions", dependencies=[Depends(require_admin_api)]
    )
    def api_broker_positions() -> dict[str, Any]:
        return _safe_broker_call("get_positions")

    @app.get(
        "/api/broker/orders", dependencies=[Depends(require_admin_api)]
    )
    def api_broker_orders() -> dict[str, Any]:
        return _safe_broker_call("get_orders")

    @app.get("/api/system", dependencies=[Depends(require_admin_api)])
    def api_system() -> dict[str, Any]:
        return system_summary(
            settings=settings, broker=broker, kill_switch=kill_switch
        )

    # ------------------------------------------------------------------
    # Paper-only admin actions (flatten / reset)
    #
    # These only operate on the in-memory paper broker. If the active
    # provider is topstep/tradovate they return a structured, safe
    # "not available for this provider yet" envelope at 200, never trying
    # to mutate a real broker's state.
    # ------------------------------------------------------------------

    def _paper_not_available() -> JSONResponse:
        return JSONResponse(
            status_code=200,
            content={
                "ok": False,
                "provider": broker.provider,
                "not_implemented": True,
                "status": "not_available_for_provider",
                "message": (
                    f"paper flatten/reset is not available for the "
                    f"{broker.provider} provider yet"
                ),
            },
        )

    @app.post(
        "/api/paper/flatten", dependencies=[Depends(require_admin_api)]
    )
    def api_paper_flatten() -> JSONResponse:
        if broker.provider != "paper":
            return _paper_not_available()
        return JSONResponse(
            status_code=200, content=broker.flatten_all_positions()
        )

    @app.post(
        "/api/paper/flatten/{symbol}",
        dependencies=[Depends(require_admin_api)],
    )
    def api_paper_flatten_symbol(symbol: str) -> JSONResponse:
        if broker.provider != "paper":
            return _paper_not_available()
        return JSONResponse(
            status_code=200, content=broker.flatten_position(symbol=symbol)
        )

    @app.post(
        "/api/paper/reset", dependencies=[Depends(require_admin_api)]
    )
    def api_paper_reset() -> JSONResponse:
        if broker.provider != "paper":
            return _paper_not_available()
        return JSONResponse(
            status_code=200, content=broker.reset_paper_state()
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

    @app.get("/", response_class=HTMLResponse, dependencies=[Depends(require_admin_page)])
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

    @app.get(
        "/settings/broker",
        response_class=HTMLResponse,
        dependencies=[Depends(require_admin_page)],
    )
    def page_settings_broker(request: Request) -> HTMLResponse:
        ctx = _page_ctx(request)
        configured = settings.resolved_provider
        broker_snapshot = broker_status_payload(settings=settings, broker=broker)
        # Only ask the active broker for accounts when it's paper — paper's
        # call is in-memory and cheap. Topstep would otherwise blow out to
        # the network on every page render; the UI has a dedicated "Fetch
        # accounts" button that goes through /api/topstep/accounts.
        if broker.provider == "paper":
            accounts = _safe_broker_call("get_accounts")
        else:
            accounts = {
                "ok": False,
                "provider": broker.provider,
                "not_implemented": True,
                "status": "not_loaded_for_this_provider",
                "accounts": [],
                "message": "paper account snapshot only shown when paper is active",
            }
        api_key = settings.topstep_api_key or ""
        api_key_preview = (
            f"…{api_key[-4:]}" if len(api_key) >= 4 else ""
        )
        ctx.update(
            {
                "topstep": {
                    "username": settings.topstep_username or "",
                    "username_set": bool(settings.topstep_username),
                    "username_preview": _mask_identifier(settings.topstep_username),
                    "password_set": bool(settings.topstep_password),
                    "api_key_set": bool(api_key),
                    "api_key_preview": api_key_preview or (
                        "configured" if api_key else ""
                    ),
                    "account_id": settings.topstep_account_id,
                    "env": settings.topstep_env,
                    "base_url": settings.topstep_base_url,
                    "ws_url": settings.topstep_ws_url,
                    "env_options": ["demo"],
                    "token_cached": bool(settings.topstep_token),
                    "token_expires_at": settings.topstep_token_expires_at or "",
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

    _TOPSTEP_API_KEY_UNCHANGED = "__topstep_api_key_unchanged__"

    @app.post(
        "/settings/broker",
        dependencies=[Depends(require_admin_page)],
    )
    def post_settings_broker(
        broker_provider: str = Form(...),
        execution_mode: str = Form(...),
        selected_account_id: str = Form(""),
        topstep_username: str = Form(""),
        topstep_api_key: str = Form(_TOPSTEP_API_KEY_UNCHANGED),
        topstep_account_id: str = Form(""),
        topstep_env: str = Form("demo"),
        topstep_base_url: str = Form("https://api.topstepx.com"),
        topstep_ws_url: str = Form("https://rtc.topstepx.com"),
    ):
        # Build the update list dynamically so the API key is only touched
        # when the user actually changed it (blank-on-purpose still clears
        # it via the sentinel).
        updates: list[tuple[str, Any]] = [
            ("BROKER_PROVIDER", broker_provider),
            ("EXECUTION_MODE", execution_mode),
            ("SELECTED_ACCOUNT_ID", selected_account_id),
            ("TOPSTEP_USERNAME", topstep_username),
            ("TOPSTEP_ACCOUNT_ID", topstep_account_id),
            ("TOPSTEP_ENV", topstep_env),
            ("TOPSTEP_BASE_URL", topstep_base_url),
            ("TOPSTEP_WS_URL", topstep_ws_url),
        ]
        if topstep_api_key != _TOPSTEP_API_KEY_UNCHANGED:
            updates.append(("TOPSTEP_API_KEY", topstep_api_key))

        try:
            coerced: dict[str, Any] = {
                key: settings_store.update_typed(key, value)
                for key, value in updates
            }
        except SettingsValidationError as exc:
            return _flash_redirect("/settings/broker", str(exc), kind="error")

        for key, value in coerced.items():
            settings_store.apply_to_settings(settings, key, value)

        msg = "Broker settings saved."
        if coerced["BROKER_PROVIDER"] != broker.provider:
            msg += " Restart required to switch the active adapter."
        return _flash_redirect("/settings/broker", msg, kind="ok")

    @app.get(
        "/settings/risk",
        response_class=HTMLResponse,
        dependencies=[Depends(require_admin_page)],
    )
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

    @app.post(
        "/settings/risk",
        dependencies=[Depends(require_admin_page)],
    )
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

    @app.get(
        "/tradingview",
        response_class=HTMLResponse,
        dependencies=[Depends(require_admin_page)],
    )
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

    @app.post(
        "/tradingview/secret",
        dependencies=[Depends(require_admin_page)],
    )
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

    @app.post(
        "/tradingview/secret/regenerate",
        dependencies=[Depends(require_admin_page)],
    )
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

    @app.get(
        "/journal",
        response_class=HTMLResponse,
        dependencies=[Depends(require_admin_page)],
    )
    def page_journal(request: Request) -> HTMLResponse:
        ctx = _page_ctx(request)
        ctx.update(journal_view(journal=journal, limit=100))
        return templates.TemplateResponse(request, "journal.html", ctx)

    @app.get(
        "/metrics",
        response_class=HTMLResponse,
        dependencies=[Depends(require_admin_page)],
    )
    def page_metrics(request: Request) -> HTMLResponse:
        ctx = _page_ctx(request)
        ctx["m"] = metrics_summary(journal=journal)
        return templates.TemplateResponse(request, "metrics.html", ctx)

    @app.get(
        "/logs",
        response_class=HTMLResponse,
        dependencies=[Depends(require_admin_page)],
    )
    def page_logs(request: Request) -> HTMLResponse:
        ctx = _page_ctx(request)
        ctx["log_path"] = str(settings.log_abs_path)
        ctx["lines"] = tail_log(settings.log_abs_path, lines=300)
        return templates.TemplateResponse(request, "logs.html", ctx)

    @app.get(
        "/system",
        response_class=HTMLResponse,
        dependencies=[Depends(require_admin_page)],
    )
    def page_system(request: Request) -> HTMLResponse:
        ctx = _page_ctx(request)
        ctx["sys"] = system_summary(
            settings=settings, broker=broker, kill_switch=kill_switch
        )
        return templates.TemplateResponse(request, "system.html", ctx)

    # ------------------------------------------------------------------
    # Auth pages (public — required to reach the protected pages above)
    # ------------------------------------------------------------------

    @app.get("/login", response_class=HTMLResponse)
    def page_login(request: Request, next: str = "/") -> Any:
        if not settings.admin_auth_enabled:
            return RedirectResponse(url=safe_next_path(next), status_code=303)
        ctx = {
            "request": request,
            "app_name": settings.app_name,
            "app_version": __version__,
            "next": safe_next_path(next),
            "error": request.query_params.get("error"),
        }
        return templates.TemplateResponse(request, "login.html", ctx)

    @app.post("/login")
    def do_login(
        request: Request,
        username: str = Form(""),
        password: str = Form(""),
        next: str = Form("/"),
    ):
        target = safe_next_path(next)
        if not settings.admin_auth_enabled:
            return RedirectResponse(url=target, status_code=303)
        if check_credentials(settings, username, password):
            auth_login(request)
            return RedirectResponse(url=target, status_code=303)
        from urllib.parse import urlencode
        qs = urlencode({"error": "invalid", "next": target})
        return RedirectResponse(url=f"/login?{qs}", status_code=303)

    @app.post("/logout")
    def do_logout(request: Request):
        auth_logout(request)
        return RedirectResponse(url="/login", status_code=303)

    @app.get("/logout")
    def do_logout_get(request: Request):
        auth_logout(request)
        return RedirectResponse(url="/login", status_code=303)

    return app


app = create_app()
