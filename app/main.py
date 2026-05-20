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
from typing import Any, List, Optional

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import __version__
from .auth import (
    LoginRequired,
    check_credentials,
    hash_password,
    login as auth_login,
    logout as auth_logout,
    require_admin_api,
    require_admin_page,
    safe_next_path,
    warn_if_default_secrets,
)
from .config import Settings, enforce_boot_validation, get_settings
from .rate_limiter import TokenBucket
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
from .symbol_map import SymbolMap, parse_form_mappings
from .webhook import WebhookHandler


_HERE = Path(__file__).resolve().parent
TEMPLATES_DIR = _HERE / "templates"
STATIC_DIR = _HERE / "static"


_DEMO_CONFIRM_TOKEN = "DEMO_ONLY"
_LIVE_CONFIRM_TOKEN = "LIVE_CONFIRMED"
# Short, deliberate phrase the operator types in the UI to engage live
# trading. The endpoint validates this typed value, then writes the
# longer ``_LIVE_STORED_TOKEN`` into ``LIVE_TRADING_CONFIRM`` — so the
# in-memory broker safety check (which expects the long token) and the
# audit log keep their grep-stable string while the UX uses a short
# phrase.
_LIVE_CONFIRMATION_PHRASE = "engage"
_LIVE_STORED_TOKEN = "I_UNDERSTAND_LIVE_ORDERS"


def _live_execution_view(
    *,
    settings: Settings,
    broker_snapshot: dict[str, Any],
    kill_switch,
) -> dict[str, Any]:
    """Build the Topstep Live Execution status panel payload.

    Surfaces every live-trading gate in one place. Visually distinct
    from the demo panel — the dashboard styles this section as warning
    so the operator cannot mistake it for the demo controls.
    """
    selected_account_id = settings.resolved_account_id or ""
    selected_account_name = broker_snapshot.get("selected_account_name")
    can_trade = broker_snapshot.get("can_trade")

    is_armed = (
        settings.resolved_provider == "topstep"
        and settings.execution_mode == "live"
        and bool(settings.enable_topstep_order_execution)
        and (settings.topstep_execution_confirm or "") == _LIVE_CONFIRM_TOKEN
        and bool(settings.enable_live_trading)
        and (settings.live_trading_confirm or "") == _LIVE_STORED_TOKEN
        and bool(settings.live_trading_account_ack)
        and bool(selected_account_id)
        and not (
            settings.live_require_kill_switch_off and kill_switch.is_active()
        )
    )

    blockers: list[str] = []
    if settings.resolved_provider != "topstep":
        blockers.append(
            f"BROKER_PROVIDER is {settings.resolved_provider!r} (need 'topstep')"
        )
    if not selected_account_id:
        blockers.append("no selected account id")
    if settings.live_require_kill_switch_off and kill_switch.is_active():
        blockers.append("kill switch is active")
    if can_trade is False:
        blockers.append("selected account canTrade=false")

    state_label = "Live Armed" if is_armed else "Live Locked"
    state_kind = "warn" if is_armed else "good"

    return {
        "state_label": state_label,
        "state_kind": state_kind,
        "is_armed": is_armed,
        "broker_provider": settings.resolved_provider,
        "execution_mode": settings.execution_mode,
        "enable_topstep_order_execution": (
            settings.enable_topstep_order_execution
        ),
        "topstep_execution_confirm": settings.topstep_execution_confirm,
        "enable_live_trading": settings.enable_live_trading,
        "live_trading_confirm": settings.live_trading_confirm,
        "live_trading_account_ack": settings.live_trading_account_ack,
        "live_max_contracts_per_trade": settings.live_max_contracts_per_trade,
        "live_allowed_symbols": list(settings.live_allowed_symbols),
        "live_require_kill_switch_off": (
            settings.live_require_kill_switch_off
        ),
        "selected_account_id": selected_account_id or None,
        "selected_account_name": selected_account_name,
        "can_trade": can_trade,
        "kill_switch_active": kill_switch.is_active(),
        "blockers": blockers,
        "confirm_phrase": _LIVE_CONFIRMATION_PHRASE,
        "confirm_token": _LIVE_CONFIRM_TOKEN,
    }


def _demo_execution_view(
    *,
    settings: Settings,
    broker_snapshot: dict[str, Any],
    kill_switch,
) -> dict[str, Any]:
    """Build the Topstep Demo Execution status panel payload.

    Surfaces every safety switch in one place plus a derived state
    label (Dry Run Active / Demo Execution Armed / Live Locked). The
    Enable button is only offered when every prerequisite except the
    confirm token is met — flipping the confirm token to ``DEMO_ONLY``
    is what arms execution.
    """
    selected_account_id = settings.resolved_account_id or ""
    selected_account_name = broker_snapshot.get("selected_account_name")
    can_trade = broker_snapshot.get("can_trade")

    is_live_locked = (
        settings.execution_mode == "live"
        or bool(settings.enable_live_trading)
    )
    is_armed = (
        settings.resolved_provider == "topstep"
        and settings.execution_mode == "demo"
        and bool(settings.enable_topstep_order_execution)
        and (settings.topstep_execution_confirm or "") == _DEMO_CONFIRM_TOKEN
        and bool(selected_account_id)
        and not is_live_locked
        and not kill_switch.is_active()
    )
    if is_live_locked:
        state_label = "Live Locked"
        state_kind = "bad"
    elif is_armed:
        state_label = "Demo Execution Armed"
        state_kind = "warn"
    else:
        state_label = "Dry Run Active"
        state_kind = "good"

    reasons_to_block: list[str] = []
    if settings.resolved_provider != "topstep":
        reasons_to_block.append(
            f"BROKER_PROVIDER is {settings.resolved_provider!r} (need 'topstep')"
        )
    if settings.execution_mode != "demo":
        reasons_to_block.append(
            f"EXECUTION_MODE is {settings.execution_mode!r} (need 'demo')"
        )
    if not selected_account_id:
        reasons_to_block.append("no selected account id")
    if is_live_locked:
        reasons_to_block.append(
            "live mode/kill is set — cannot arm demo execution"
        )
    if kill_switch.is_active():
        reasons_to_block.append("kill switch is active")

    can_enable = not is_live_locked and not kill_switch.is_active()

    return {
        "state_label": state_label,
        "state_kind": state_kind,
        "is_armed": is_armed,
        "is_live_locked": is_live_locked,
        "broker_provider": settings.resolved_provider,
        "execution_mode": settings.execution_mode,
        "enable_topstep_order_execution": (
            settings.enable_topstep_order_execution
        ),
        "topstep_execution_confirm": settings.topstep_execution_confirm,
        "enable_live_trading": settings.enable_live_trading,
        "selected_account_id": selected_account_id or None,
        "selected_account_name": selected_account_name,
        "can_trade": can_trade,
        "kill_switch_active": kill_switch.is_active(),
        "can_enable": can_enable,
        "blockers": reasons_to_block,
        "confirm_token": _DEMO_CONFIRM_TOKEN,
    }


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

    # Refuse to start with a default/short/missing webhook secret. Runs
    # before any subsystem (journal, broker, routes) is constructed so a
    # misconfigured install fails fast.
    enforce_boot_validation(settings, log)

    journal = Journal(
        settings.database_abs_path,
        trading_day_timezone=settings.trading_day_timezone,
    )
    settings_store = SettingsStore(settings.database_abs_path)
    settings_store.initialize_settings_from_env(settings)
    kill_switch = KillSwitch(
        settings.database_abs_path.parent / "kill_switch.active",
        enabled=settings.enable_kill_switch,
    )
    # M2 — surface ENABLE_KILL_SWITCH=false at boot. is_active() always
    # returns False in this state, which means the dashboard kill-switch
    # toggle and the live-trading kill-switch gate both silently no-op.
    # The dashboard pill is rendered as "disabled (config)" elsewhere;
    # this warning makes the startup logs explicit too.
    if not settings.enable_kill_switch:
        log.warning(
            "ENABLE_KILL_SWITCH=false — kill switch disabled by config. "
            "Emergency-stop button will not block trades and the live "
            "kill-switch gate trivially passes. Set ENABLE_KILL_SWITCH=true "
            "to re-enable."
        )
    # Broker built first so the risk engine can consult it during
    # max_open_positions evaluation (H3).
    broker = build_broker(settings, journal, settings_store=settings_store)
    risk = RiskEngine(
        settings=settings,
        journal=journal,
        kill_switch=kill_switch,
        broker=broker,
    )
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
        # ``enforce_boot_validation`` above guarantees session_secret is
        # non-empty, not the placeholder, and at least
        # SESSION_SECRET_MIN_LENGTH characters when auth is on. No
        # silent fallback string here.
        app.add_middleware(
            SessionMiddleware,
            secret_key=settings.session_secret,
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
            strategy_managed_risk=settings.strategy_managed_risk,
            fixed_contracts_per_trade=settings.fixed_contracts_per_trade,
            max_contracts_per_trade=settings.max_contracts_per_trade,
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
        return metrics_summary(journal=journal, broker=broker)

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
            # Mirror the latest settings onto the live broker so admin
            # endpoints reflect runtime changes without a restart.
            broker.enable_order_execution = (
                settings.enable_topstep_order_execution
            )
            broker.enable_order_dry_run = settings.enable_topstep_order_dry_run
            broker.execution_confirm = settings.topstep_execution_confirm
            broker.enable_live_trading = settings.enable_live_trading
            broker.execution_mode = settings.execution_mode
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
            enable_order_execution=settings.enable_topstep_order_execution,
            enable_order_dry_run=settings.enable_topstep_order_dry_run,
            execution_confirm=settings.topstep_execution_confirm,
            enable_live_trading=settings.enable_live_trading,
            execution_mode=settings.execution_mode,
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
        "/api/topstep/build-order-preview",
        dependencies=[Depends(require_admin_api)],
    )
    async def api_topstep_build_order_preview(
        request: Request,
    ) -> JSONResponse:
        """Build a dry-run Topstep market-order payload.

        Body is an optional sample TradingViewAlert. When omitted, the
        most recent journaled signal is reused. Nothing is submitted —
        ``would_submit`` is always false. The response includes the
        normalized signal, account id, contract id, side, size, full
        order payload, and the full set of safety gates so the operator
        can see exactly why an order would or wouldn't go through.
        """
        from .risk_engine import (
            normalize_action,
            normalize_timeframe,
            parse_float as _pfloat,
            parse_int as _pint,
        )
        from .schemas import NormalizedSignal, TradingViewAlert

        try:
            body = await request.json()
        except Exception:
            body = None

        signal: Optional[NormalizedSignal] = None
        source_label = "request_body"

        def _from_payload(payload: dict[str, Any]) -> Optional[NormalizedSignal]:
            try:
                alert = TradingViewAlert.model_validate(payload)
            except Exception:
                return None
            action = normalize_action(alert.action) or alert.action
            broker_symbol = (
                symbol_map.resolve(alert.symbol, broker.provider)
                if symbol_map is not None
                else alert.symbol
            )
            return NormalizedSignal(
                source=alert.source or "tradingview",
                strategy=alert.strategy,
                symbol=alert.symbol,
                broker_symbol=broker_symbol,
                exchange=alert.exchange,
                action=action,
                contracts=_pint(alert.contracts, default=1) or 1,
                price=_pfloat(alert.price),
                order_id=alert.order_id,
                comment=alert.comment,
                timeframe=normalize_timeframe(alert.timeframe),
                raw=payload,
            )

        if isinstance(body, dict) and body.get("symbol") and body.get("action"):
            signal = _from_payload(body)
        elif isinstance(body, dict) and isinstance(body.get("alert"), dict):
            signal = _from_payload(body["alert"])
        if signal is None:
            latest = journal.latest_signal(decision="accepted") or journal.latest_signal()
            if latest is None:
                return JSONResponse(
                    status_code=200,
                    content={
                        "ok": False,
                        "status": "no_signal_available",
                        "message": (
                            "POST a TradingViewAlert JSON body, or wait for a "
                            "signal to appear in the journal."
                        ),
                        "would_submit": False,
                    },
                )
            source_label = "latest_journal_signal"
            try:
                raw = json.loads(latest["raw_payload"] or "{}")
            except (TypeError, ValueError):
                raw = {}
            if isinstance(raw, dict) and raw.get("symbol") and raw.get("action"):
                signal = _from_payload(raw)
            if signal is None:
                signal = NormalizedSignal(
                    source=latest.get("source") or "tradingview",
                    strategy=latest.get("strategy"),
                    symbol=latest.get("symbol") or "",
                    broker_symbol=latest.get("broker_symbol")
                    or (
                        symbol_map.resolve(latest.get("symbol"), broker.provider)
                        if symbol_map is not None
                        else latest.get("symbol")
                    ),
                    exchange=None,
                    action=latest.get("action") or "BUY",
                    contracts=int(latest.get("contracts") or 1),
                    price=latest.get("price"),
                    order_id=latest.get("order_id"),
                    comment=None,
                    timeframe=latest.get("timeframe"),
                    raw=raw if isinstance(raw, dict) else {},
                )

        topstep = _topstep_adapter_for_admin()
        preview = topstep.build_order_preview(signal, symbol_map=symbol_map)
        return JSONResponse(
            status_code=200,
            content={
                "ok": bool(preview.get("ok")),
                "would_submit": False,
                "execution_mode": settings.execution_mode,
                "broker_provider": broker.provider,
                "signal_source": source_label,
                "normalized_signal": {
                    "source": signal.source,
                    "strategy": signal.strategy,
                    "symbol": signal.symbol,
                    "broker_symbol": signal.broker_symbol,
                    "action": signal.action,
                    "contracts": signal.contracts,
                    "price": signal.price,
                    "order_id": signal.order_id,
                    "comment": signal.comment,
                    "timeframe": signal.timeframe,
                },
                "account_id": preview.get("account_id"),
                "contract_id": preview.get("contract_id"),
                "side": preview.get("side"),
                "size": preview.get("size"),
                "payload": preview.get("payload"),
                "reason": preview.get("reason"),
                "safety": topstep._safety_state(),
            },
        )

    @app.post(
        "/api/topstep/submit-test-order",
        dependencies=[Depends(require_admin_api)],
    )
    async def api_topstep_submit_test_order(
        request: Request,
    ) -> JSONResponse:
        """Manually submit a tiny demo/sim Topstep order.

        Strictly gated:
          * BROKER_PROVIDER must be ``topstep``
          * EXECUTION_MODE must be ``demo``
          * ENABLE_TOPSTEP_ORDER_EXECUTION must be true
          * TOPSTEP_EXECUTION_CONFIRM must be ``DEMO_ONLY``
          * ENABLE_LIVE_TRADING must be false

        ``submit_market_order`` enforces all of these; this endpoint
        merely wires up a tiny sample signal (1 contract, BUY) using the
        configured Topstep symbol map. Returns the ProjectX response
        envelope, never silently no-ops.
        """
        from .schemas import NormalizedSignal

        if broker.provider != "topstep":
            return JSONResponse(
                status_code=200,
                content={
                    "ok": False,
                    "status": "broker_provider_not_topstep",
                    "message": (
                        f"active provider is {broker.provider} — switch "
                        "BROKER_PROVIDER=topstep and restart"
                    ),
                    "would_submit": False,
                },
            )

        if settings.execution_mode == "live" or settings.enable_live_trading:
            return JSONResponse(
                status_code=200,
                content={
                    "ok": False,
                    "status": "live_execution_locked",
                    "message": (
                        "EXECUTION_MODE=live (or ENABLE_LIVE_TRADING) is "
                        "set — live execution is intentionally locked"
                    ),
                    "would_submit": False,
                },
            )

        try:
            body = await request.json()
        except Exception:
            body = None
        body = body if isinstance(body, dict) else {}

        symbol = (
            str(body.get("symbol") or "").strip()
            or (settings.allowed_symbols[0] if settings.allowed_symbols else "MES1!")
        )
        action_raw = str(body.get("action") or "BUY").strip().upper()
        if action_raw not in {"BUY", "SELL"}:
            return JSONResponse(
                status_code=400,
                content={
                    "ok": False,
                    "status": "unsupported_action",
                    "message": (
                        f"action must be BUY or SELL (got {action_raw!r})"
                    ),
                    "would_submit": False,
                },
            )

        try:
            contracts = int(body.get("contracts") or 1)
        except (TypeError, ValueError):
            return JSONResponse(
                status_code=400,
                content={
                    "ok": False,
                    "status": "invalid_contracts",
                    "message": (
                        "contracts must be a positive integer"
                    ),
                    "would_submit": False,
                },
            )
        if contracts < 1:
            return JSONResponse(
                status_code=400,
                content={
                    "ok": False,
                    "status": "invalid_contracts",
                    "message": "contracts must be >= 1",
                    "would_submit": False,
                },
            )
        if contracts > settings.max_contracts_per_trade:
            return JSONResponse(
                status_code=400,
                content={
                    "ok": False,
                    "status": "contracts_above_max",
                    "message": (
                        f"contracts={contracts} exceeds "
                        f"MAX_CONTRACTS_PER_TRADE="
                        f"{settings.max_contracts_per_trade}"
                    ),
                    "would_submit": False,
                },
            )

        # Hard reject when the operator forgot to map this ticker — the
        # builder would refuse later anyway, but failing here gives a
        # clearer 400 with a stable status label tests can assert on.
        explicit_mapping = (
            symbol_map.resolve_explicit(symbol, broker.provider)
            if symbol_map is not None
            else None
        )
        if not explicit_mapping:
            return JSONResponse(
                status_code=400,
                content={
                    "ok": False,
                    "status": "symbol_mapping_missing",
                    "message": (
                        f"Topstep contract id missing for {symbol!r} —"
                        " add it in Configuration > Symbols"
                    ),
                    "would_submit": False,
                },
            )
        broker_symbol = explicit_mapping
        signal = NormalizedSignal(
            source="manual_test",
            strategy="topstep_submit_test_order",
            symbol=symbol,
            broker_symbol=broker_symbol,
            exchange=None,
            action=action_raw,
            contracts=contracts,
            price=None,
            order_id=body.get("order_id"),
            comment="topstep_submit_test_order",
            timeframe=None,
            raw=body,
        )

        topstep = _topstep_adapter_for_admin()
        result = topstep.submit_market_order(signal, symbol_map=symbol_map)
        return JSONResponse(status_code=200, content=result)

    @app.post(
        "/api/topstep/demo-execution/enable",
        dependencies=[Depends(require_admin_api)],
    )
    async def api_topstep_demo_execution_enable(
        request: Request,
    ) -> JSONResponse:
        """Arm Topstep demo execution.

        Hard rules (any failure → 400 with structured envelope):

          * Confirmation text must equal ``DEMO_ONLY``.
          * BROKER_PROVIDER must already be ``topstep`` (we never flip it
            for the operator — that requires a restart anyway).
          * A Topstep account must be selected (numeric or otherwise).
          * EXECUTION_MODE must not be ``live`` (it's blocked at the
            settings layer too — this is defense in depth).
          * The kill switch must not be active.

        Sets:

          * ``ENABLE_TOPSTEP_ORDER_EXECUTION = true``
          * ``TOPSTEP_EXECUTION_CONFIRM = DEMO_ONLY``
          * ``EXECUTION_MODE = demo`` (only when not already live)

        Never touches ``ENABLE_LIVE_TRADING``. Live/funded execution
        stays locked.
        """
        try:
            body = await request.json()
        except Exception:
            body = None
        body = body if isinstance(body, dict) else {}
        confirm = str(body.get("confirm") or "").strip()
        if confirm != _DEMO_CONFIRM_TOKEN:
            return JSONResponse(
                status_code=400,
                content={
                    "ok": False,
                    "status": "invalid_confirmation",
                    "message": (
                        "confirmation token must equal "
                        f"{_DEMO_CONFIRM_TOKEN!r}"
                    ),
                },
            )

        if settings.resolved_provider != "topstep":
            return JSONResponse(
                status_code=400,
                content={
                    "ok": False,
                    "status": "broker_provider_not_topstep",
                    "message": (
                        f"BROKER_PROVIDER is {settings.resolved_provider!r}"
                        " — set it to 'topstep' before arming demo execution"
                    ),
                },
            )

        selected_id = settings.resolved_account_id or ""
        if not selected_id:
            return JSONResponse(
                status_code=400,
                content={
                    "ok": False,
                    "status": "no_selected_account",
                    "message": (
                        "no Topstep account selected — set "
                        "SELECTED_ACCOUNT_ID / TOPSTEP_ACCOUNT_ID first"
                    ),
                },
            )

        if settings.execution_mode == "live":
            return JSONResponse(
                status_code=400,
                content={
                    "ok": False,
                    "status": "execution_mode_live_blocked",
                    "message": (
                        "EXECUTION_MODE=live is blocked — cannot arm demo"
                    ),
                },
            )

        if settings.enable_live_trading:
            return JSONResponse(
                status_code=400,
                content={
                    "ok": False,
                    "status": "live_trading_locked",
                    "message": (
                        "ENABLE_LIVE_TRADING is true (locked) — refusing "
                        "to touch execution settings"
                    ),
                },
            )

        if kill_switch.is_active():
            return JSONResponse(
                status_code=400,
                content={
                    "ok": False,
                    "status": "kill_switch_active",
                    "message": (
                        "kill switch is active — deactivate it before "
                        "arming demo execution"
                    ),
                },
            )

        try:
            execution_mode = settings_store.update_typed(
                "EXECUTION_MODE", "demo"
            )
            order_exec_flag = settings_store.update_typed(
                "ENABLE_TOPSTEP_ORDER_EXECUTION", "true"
            )
            confirm_token = settings_store.update_typed(
                "TOPSTEP_EXECUTION_CONFIRM", _DEMO_CONFIRM_TOKEN
            )
        except SettingsValidationError as exc:
            return JSONResponse(
                status_code=400,
                content={
                    "ok": False,
                    "status": "settings_error",
                    "message": str(exc),
                },
            )

        settings_store.apply_to_settings(
            settings, "EXECUTION_MODE", execution_mode
        )
        settings_store.apply_to_settings(
            settings, "ENABLE_TOPSTEP_ORDER_EXECUTION", order_exec_flag
        )
        settings_store.apply_to_settings(
            settings, "TOPSTEP_EXECUTION_CONFIRM", confirm_token
        )
        # Mirror onto the live broker so the next webhook reflects the
        # new state without a restart.
        if isinstance(broker, TopstepBroker):
            broker.execution_mode = execution_mode
            broker.enable_order_execution = order_exec_flag
            broker.execution_confirm = confirm_token

        log.info(
            "topstep demo execution armed: provider=%s mode=%s account=%s",
            settings.resolved_provider,
            settings.execution_mode,
            settings.resolved_account_id,
        )
        return JSONResponse(
            status_code=200,
            content={
                "ok": True,
                "status": "demo_execution_armed",
                "broker_provider": settings.resolved_provider,
                "execution_mode": settings.execution_mode,
                "enable_topstep_order_execution": (
                    settings.enable_topstep_order_execution
                ),
                "topstep_execution_confirm": (
                    settings.topstep_execution_confirm
                ),
                "enable_live_trading": settings.enable_live_trading,
                "selected_account_id": settings.resolved_account_id or None,
                "message": (
                    "Demo execution armed. Live/funded execution is "
                    "still locked."
                ),
            },
        )

    @app.post(
        "/api/topstep/demo-execution/disable",
        dependencies=[Depends(require_admin_api)],
    )
    def api_topstep_demo_execution_disable() -> JSONResponse:
        """Disarm Topstep demo execution.

        Sets ``ENABLE_TOPSTEP_ORDER_EXECUTION=false`` and
        ``TOPSTEP_EXECUTION_CONFIRM=disabled``. Provider and account
        stay where they are.
        """
        try:
            order_exec_flag = settings_store.update_typed(
                "ENABLE_TOPSTEP_ORDER_EXECUTION", "false"
            )
            confirm_token = settings_store.update_typed(
                "TOPSTEP_EXECUTION_CONFIRM", "disabled"
            )
        except SettingsValidationError as exc:
            return JSONResponse(
                status_code=400,
                content={
                    "ok": False,
                    "status": "settings_error",
                    "message": str(exc),
                },
            )

        settings_store.apply_to_settings(
            settings, "ENABLE_TOPSTEP_ORDER_EXECUTION", order_exec_flag
        )
        settings_store.apply_to_settings(
            settings, "TOPSTEP_EXECUTION_CONFIRM", confirm_token
        )
        if isinstance(broker, TopstepBroker):
            broker.enable_order_execution = order_exec_flag
            broker.execution_confirm = confirm_token

        log.info(
            "topstep demo execution disabled: provider=%s mode=%s",
            settings.resolved_provider,
            settings.execution_mode,
        )
        return JSONResponse(
            status_code=200,
            content={
                "ok": True,
                "status": "demo_execution_disabled",
                "broker_provider": settings.resolved_provider,
                "execution_mode": settings.execution_mode,
                "enable_topstep_order_execution": (
                    settings.enable_topstep_order_execution
                ),
                "topstep_execution_confirm": (
                    settings.topstep_execution_confirm
                ),
                "enable_live_trading": settings.enable_live_trading,
                "message": (
                    "Demo execution disabled. Topstep webhooks build "
                    "dry-run previews only."
                ),
            },
        )

    @app.post(
        "/api/topstep/live-execution/enable",
        dependencies=[Depends(require_admin_api)],
    )
    async def api_topstep_live_execution_enable(
        request: Request,
    ) -> JSONResponse:
        """Arm Topstep LIVE/funded execution.

        Requires the operator to type the exact confirmation phrase
        ``engage`` and explicitly acknowledge the selected account.
        Flips every gate together so a future request sees a consistent
        state. Never touches credentials/tokens. The persisted
        ``LIVE_TRADING_CONFIRM`` value is the long-form internal token
        the broker safety check expects — the typed phrase is the
        UX-facing surface, not the storage layer.
        """
        try:
            body = await request.json()
        except Exception:
            body = None
        body = body if isinstance(body, dict) else {}
        confirm = str(body.get("confirm") or "").strip()
        account_ack = body.get("account_ack")
        if isinstance(account_ack, str):
            ack = account_ack.strip().lower() in {"true", "1", "yes", "on"}
        else:
            ack = bool(account_ack)

        if confirm != _LIVE_CONFIRMATION_PHRASE:
            return JSONResponse(
                status_code=400,
                content={
                    "ok": False,
                    "status": "invalid_confirmation",
                    "message": (
                        "confirmation phrase must equal "
                        f"{_LIVE_CONFIRMATION_PHRASE!r}"
                    ),
                },
            )

        if not ack:
            return JSONResponse(
                status_code=400,
                content={
                    "ok": False,
                    "status": "account_ack_missing",
                    "message": (
                        "account acknowledgement must be true to arm live"
                    ),
                },
            )

        if settings.resolved_provider != "topstep":
            return JSONResponse(
                status_code=400,
                content={
                    "ok": False,
                    "status": "broker_provider_not_topstep",
                    "message": (
                        f"BROKER_PROVIDER is {settings.resolved_provider!r}"
                        " — set it to 'topstep' before arming live execution"
                    ),
                },
            )

        if not (settings.resolved_account_id or ""):
            return JSONResponse(
                status_code=400,
                content={
                    "ok": False,
                    "status": "no_selected_account",
                    "message": (
                        "no Topstep account selected — set "
                        "SELECTED_ACCOUNT_ID / TOPSTEP_ACCOUNT_ID first"
                    ),
                },
            )

        if (
            settings.live_require_kill_switch_off
            and kill_switch.is_active()
        ):
            return JSONResponse(
                status_code=400,
                content={
                    "ok": False,
                    "status": "kill_switch_active",
                    "message": (
                        "kill switch is active — deactivate it before "
                        "arming live execution"
                    ),
                },
            )

        try:
            execution_mode = settings_store.update_typed(
                "EXECUTION_MODE", "live"
            )
            order_exec_flag = settings_store.update_typed(
                "ENABLE_TOPSTEP_ORDER_EXECUTION", "true"
            )
            confirm_token = settings_store.update_typed(
                "TOPSTEP_EXECUTION_CONFIRM", _LIVE_CONFIRM_TOKEN
            )
            live_flag = settings_store.update_typed(
                "ENABLE_LIVE_TRADING", "true"
            )
            live_confirm = settings_store.update_typed(
                "LIVE_TRADING_CONFIRM", _LIVE_STORED_TOKEN
            )
            live_ack = settings_store.update_typed(
                "LIVE_TRADING_ACCOUNT_ACK", "true"
            )
        except SettingsValidationError as exc:
            return JSONResponse(
                status_code=400,
                content={
                    "ok": False,
                    "status": "settings_error",
                    "message": str(exc),
                },
            )

        settings_store.apply_to_settings(
            settings, "EXECUTION_MODE", execution_mode
        )
        settings_store.apply_to_settings(
            settings, "ENABLE_TOPSTEP_ORDER_EXECUTION", order_exec_flag
        )
        settings_store.apply_to_settings(
            settings, "TOPSTEP_EXECUTION_CONFIRM", confirm_token
        )
        settings_store.apply_to_settings(
            settings, "ENABLE_LIVE_TRADING", live_flag
        )
        settings_store.apply_to_settings(
            settings, "LIVE_TRADING_CONFIRM", live_confirm
        )
        settings_store.apply_to_settings(
            settings, "LIVE_TRADING_ACCOUNT_ACK", live_ack
        )

        if isinstance(broker, TopstepBroker):
            broker.execution_mode = execution_mode
            broker.enable_order_execution = order_exec_flag
            broker.execution_confirm = confirm_token
            broker.enable_live_trading = live_flag
            broker.live_trading_confirm = live_confirm
            broker.live_trading_account_ack = live_ack

        # Audit log — no secrets, no token, no password.
        log.warning(
            "TOPSTEP LIVE EXECUTION ARMED: provider=%s mode=%s "
            "account=%s live_max_contracts=%s allowed_symbols=%s",
            settings.resolved_provider,
            settings.execution_mode,
            settings.resolved_account_id,
            settings.live_max_contracts_per_trade,
            ",".join(settings.live_allowed_symbols),
        )
        try:
            journal.record_signal(
                source="admin",
                strategy="live_execution_arm",
                symbol=None,
                action="LIVE_ARMED",
                contracts=None,
                price=None,
                order_id=None,
                raw_payload={
                    "event": "live_execution_armed",
                    "broker_provider": settings.resolved_provider,
                    "execution_mode": settings.execution_mode,
                    "account_id": settings.resolved_account_id or None,
                },
                decision="accepted",
                rejection_reason=None,
                execution_mode=settings.execution_mode,
                execution_result={
                    "event": "live_execution_armed",
                    "live_max_contracts_per_trade": (
                        settings.live_max_contracts_per_trade
                    ),
                    "live_allowed_symbols": list(
                        settings.live_allowed_symbols
                    ),
                },
                broker_provider=settings.resolved_provider,
                broker_symbol=None,
                timeframe=None,
            )
        except Exception:  # pragma: no cover - persistence best-effort
            # L1 — don't swallow silently. The arming response still
            # returns success because the in-memory + settings_store
            # write already completed; the journal is for audit only.
            # But the operator should see the audit-trail gap in logs.
            log.warning(
                "live-execution arm: audit journal write failed",
                exc_info=True,
            )

        return JSONResponse(
            status_code=200,
            content={
                "ok": True,
                "status": "live_execution_armed",
                "broker_provider": settings.resolved_provider,
                "execution_mode": settings.execution_mode,
                "enable_topstep_order_execution": (
                    settings.enable_topstep_order_execution
                ),
                "topstep_execution_confirm": (
                    settings.topstep_execution_confirm
                ),
                "enable_live_trading": settings.enable_live_trading,
                "live_trading_confirm": settings.live_trading_confirm,
                "live_trading_account_ack": (
                    settings.live_trading_account_ack
                ),
                "live_max_contracts_per_trade": (
                    settings.live_max_contracts_per_trade
                ),
                "live_allowed_symbols": list(
                    settings.live_allowed_symbols
                ),
                "selected_account_id": settings.resolved_account_id or None,
                "message": (
                    "Live execution armed. Submitted orders will hit "
                    "/api/Order/place on the funded account — proceed "
                    "with care."
                ),
            },
        )

    @app.post(
        "/api/topstep/live-execution/disable",
        dependencies=[Depends(require_admin_api)],
    )
    def api_topstep_live_execution_disable() -> JSONResponse:
        """Disarm Topstep live execution — flip every live-relevant gate
        back to the safe default in one shot. ``EXECUTION_MODE`` is
        rolled back to ``demo`` so a subsequent demo arm path works
        without re-flipping settings manually."""
        try:
            live_flag = settings_store.update_typed(
                "ENABLE_LIVE_TRADING", "false"
            )
            live_confirm = settings_store.update_typed(
                "LIVE_TRADING_CONFIRM", "disabled"
            )
            live_ack = settings_store.update_typed(
                "LIVE_TRADING_ACCOUNT_ACK", "false"
            )
            confirm_token = settings_store.update_typed(
                "TOPSTEP_EXECUTION_CONFIRM", "disabled"
            )
            order_exec_flag = settings_store.update_typed(
                "ENABLE_TOPSTEP_ORDER_EXECUTION", "false"
            )
            execution_mode = settings_store.update_typed(
                "EXECUTION_MODE", "demo"
            )
        except SettingsValidationError as exc:
            return JSONResponse(
                status_code=400,
                content={
                    "ok": False,
                    "status": "settings_error",
                    "message": str(exc),
                },
            )

        settings_store.apply_to_settings(
            settings, "ENABLE_LIVE_TRADING", live_flag
        )
        settings_store.apply_to_settings(
            settings, "LIVE_TRADING_CONFIRM", live_confirm
        )
        settings_store.apply_to_settings(
            settings, "LIVE_TRADING_ACCOUNT_ACK", live_ack
        )
        settings_store.apply_to_settings(
            settings, "TOPSTEP_EXECUTION_CONFIRM", confirm_token
        )
        settings_store.apply_to_settings(
            settings, "ENABLE_TOPSTEP_ORDER_EXECUTION", order_exec_flag
        )
        settings_store.apply_to_settings(
            settings, "EXECUTION_MODE", execution_mode
        )
        if isinstance(broker, TopstepBroker):
            broker.enable_live_trading = live_flag
            broker.live_trading_confirm = live_confirm
            broker.live_trading_account_ack = live_ack
            broker.execution_confirm = confirm_token
            broker.enable_order_execution = order_exec_flag
            broker.execution_mode = execution_mode

        log.warning(
            "TOPSTEP LIVE EXECUTION DISARMED: provider=%s mode=%s",
            settings.resolved_provider,
            settings.execution_mode,
        )
        try:
            journal.record_signal(
                source="admin",
                strategy="live_execution_disarm",
                symbol=None,
                action="LIVE_DISARMED",
                contracts=None,
                price=None,
                order_id=None,
                raw_payload={"event": "live_execution_disarmed"},
                decision="accepted",
                rejection_reason=None,
                execution_mode=settings.execution_mode,
                execution_result={"event": "live_execution_disarmed"},
                broker_provider=settings.resolved_provider,
                broker_symbol=None,
                timeframe=None,
            )
        except Exception:  # pragma: no cover - audit only
            pass

        return JSONResponse(
            status_code=200,
            content={
                "ok": True,
                "status": "live_execution_disabled",
                "broker_provider": settings.resolved_provider,
                "execution_mode": settings.execution_mode,
                "enable_topstep_order_execution": (
                    settings.enable_topstep_order_execution
                ),
                "topstep_execution_confirm": (
                    settings.topstep_execution_confirm
                ),
                "enable_live_trading": settings.enable_live_trading,
                "live_trading_confirm": settings.live_trading_confirm,
                "live_trading_account_ack": (
                    settings.live_trading_account_ack
                ),
                "message": (
                    "Live execution disarmed. Submitted webhooks now "
                    "build dry-run previews until live is re-armed."
                ),
            },
        )

    @app.post(
        "/api/topstep/live-execution/verify",
        dependencies=[Depends(require_admin_api)],
    )
    def api_topstep_live_execution_verify() -> JSONResponse:
        """Preview every live-execution gate without enabling live.

        Used by the Dashboard live-engagement animation: it gives the UI
        a deterministic list of failed gates before any settings flip.
        Never mutates settings. Always returns 200 with a structured
        envelope so the UI can render reasons inline.
        """
        broker_snapshot = broker_status_payload(
            settings=settings, broker=broker
        )
        live_view = _live_execution_view(
            settings=settings,
            broker_snapshot=broker_snapshot,
            kill_switch=kill_switch,
        )
        selected_account_id = settings.resolved_account_id or ""
        selected_account_name = broker_snapshot.get("selected_account_name")
        can_trade = broker_snapshot.get("can_trade")

        failed_gates: list[str] = []
        if settings.resolved_provider != "topstep":
            failed_gates.append("broker_provider_not_topstep")
        if not selected_account_id:
            failed_gates.append("no_selected_account")
        if (
            settings.live_require_kill_switch_off
            and kill_switch.is_active()
        ):
            failed_gates.append("kill_switch_active")
        if can_trade is False:
            failed_gates.append("account_can_trade_false")
        if not settings.live_allowed_symbols:
            failed_gates.append("no_live_allowed_symbols")
        if (settings.live_max_contracts_per_trade or 0) < 1:
            failed_gates.append("live_max_contracts_invalid")

        ok = not failed_gates
        return JSONResponse(
            status_code=200,
            content={
                "ok": ok,
                "status": "live_gates_ok" if ok else "live_gates_failed",
                "failed_gates": failed_gates,
                "selected_account_id": selected_account_id or None,
                "account_name": selected_account_name,
                "canTrade": can_trade,
                "kill_switch": kill_switch.is_active(),
                "live_allowed_symbols": list(settings.live_allowed_symbols),
                "live_max_contracts": settings.live_max_contracts_per_trade,
                "blockers": live_view.get("blockers", []),
            },
        )

    @app.post(
        "/api/execution/apply-mode",
        dependencies=[Depends(require_admin_api)],
    )
    async def api_execution_apply_mode(
        request: Request,
    ) -> JSONResponse:
        """Apply EXECUTION_MODE from the Dashboard mode dropdown.

        Accepts ``paper`` and ``demo`` directly — flipping flags as the
        old dashboard arm path would. ``live`` is rejected here because
        live arming requires the dedicated verify + enable flow with the
        confirmation phrase and account acknowledgement.
        """
        try:
            body = await request.json()
        except Exception:
            body = None
        body = body if isinstance(body, dict) else {}
        mode = str(body.get("mode") or "").strip().lower()
        if mode not in {"paper", "demo"}:
            return JSONResponse(
                status_code=400,
                content={
                    "ok": False,
                    "status": "invalid_mode",
                    "message": (
                        "mode must be 'paper' or 'demo' — use the live "
                        "engagement flow for live"
                    ),
                },
            )

        if mode == "paper":
            try:
                execution_mode = settings_store.update_typed(
                    "EXECUTION_MODE", "paper"
                )
                order_exec_flag = settings_store.update_typed(
                    "ENABLE_TOPSTEP_ORDER_EXECUTION", "false"
                )
                confirm_token = settings_store.update_typed(
                    "TOPSTEP_EXECUTION_CONFIRM", "disabled"
                )
                live_flag = settings_store.update_typed(
                    "ENABLE_LIVE_TRADING", "false"
                )
                live_confirm = settings_store.update_typed(
                    "LIVE_TRADING_CONFIRM", "disabled"
                )
                live_ack = settings_store.update_typed(
                    "LIVE_TRADING_ACCOUNT_ACK", "false"
                )
            except SettingsValidationError as exc:
                return JSONResponse(
                    status_code=400,
                    content={
                        "ok": False,
                        "status": "settings_error",
                        "message": str(exc),
                    },
                )
            for key, value in (
                ("EXECUTION_MODE", execution_mode),
                ("ENABLE_TOPSTEP_ORDER_EXECUTION", order_exec_flag),
                ("TOPSTEP_EXECUTION_CONFIRM", confirm_token),
                ("ENABLE_LIVE_TRADING", live_flag),
                ("LIVE_TRADING_CONFIRM", live_confirm),
                ("LIVE_TRADING_ACCOUNT_ACK", live_ack),
            ):
                settings_store.apply_to_settings(settings, key, value)
            if isinstance(broker, TopstepBroker):
                broker.execution_mode = execution_mode
                broker.enable_order_execution = order_exec_flag
                broker.execution_confirm = confirm_token
                broker.enable_live_trading = live_flag
                broker.live_trading_confirm = live_confirm
                broker.live_trading_account_ack = live_ack
            log.info("execution apply-mode=paper applied")
            return JSONResponse(
                status_code=200,
                content={
                    "ok": True,
                    "status": "execution_mode_paper",
                    "execution_mode": settings.execution_mode,
                    "enable_topstep_order_execution": (
                        settings.enable_topstep_order_execution
                    ),
                    "enable_live_trading": settings.enable_live_trading,
                    "message": "Execution returned to dry-run / paper.",
                },
            )

        # mode == "demo" — same gates as the legacy demo-execution arm
        # path, but no manual phrase entry.
        if settings.resolved_provider != "topstep":
            return JSONResponse(
                status_code=400,
                content={
                    "ok": False,
                    "status": "broker_provider_not_topstep",
                    "message": (
                        f"BROKER_PROVIDER is {settings.resolved_provider!r}"
                        " — set it to 'topstep' before applying demo mode"
                    ),
                },
            )
        if not (settings.resolved_account_id or ""):
            return JSONResponse(
                status_code=400,
                content={
                    "ok": False,
                    "status": "no_selected_account",
                    "message": (
                        "no Topstep account selected — set "
                        "SELECTED_ACCOUNT_ID / TOPSTEP_ACCOUNT_ID first"
                    ),
                },
            )
        if settings.enable_live_trading:
            return JSONResponse(
                status_code=400,
                content={
                    "ok": False,
                    "status": "live_trading_locked",
                    "message": (
                        "ENABLE_LIVE_TRADING is true — disable live first"
                    ),
                },
            )
        if kill_switch.is_active():
            return JSONResponse(
                status_code=400,
                content={
                    "ok": False,
                    "status": "kill_switch_active",
                    "message": (
                        "kill switch is active — deactivate it before "
                        "applying demo mode"
                    ),
                },
            )
        try:
            execution_mode = settings_store.update_typed(
                "EXECUTION_MODE", "demo"
            )
            order_exec_flag = settings_store.update_typed(
                "ENABLE_TOPSTEP_ORDER_EXECUTION", "true"
            )
            confirm_token = settings_store.update_typed(
                "TOPSTEP_EXECUTION_CONFIRM", _DEMO_CONFIRM_TOKEN
            )
        except SettingsValidationError as exc:
            return JSONResponse(
                status_code=400,
                content={
                    "ok": False,
                    "status": "settings_error",
                    "message": str(exc),
                },
            )
        for key, value in (
            ("EXECUTION_MODE", execution_mode),
            ("ENABLE_TOPSTEP_ORDER_EXECUTION", order_exec_flag),
            ("TOPSTEP_EXECUTION_CONFIRM", confirm_token),
        ):
            settings_store.apply_to_settings(settings, key, value)
        if isinstance(broker, TopstepBroker):
            broker.execution_mode = execution_mode
            broker.enable_order_execution = order_exec_flag
            broker.execution_confirm = confirm_token
        log.info(
            "execution apply-mode=demo applied: account=%s",
            settings.resolved_account_id,
        )
        return JSONResponse(
            status_code=200,
            content={
                "ok": True,
                "status": "execution_mode_demo",
                "execution_mode": settings.execution_mode,
                "enable_topstep_order_execution": (
                    settings.enable_topstep_order_execution
                ),
                "topstep_execution_confirm": (
                    settings.topstep_execution_confirm
                ),
                "enable_live_trading": settings.enable_live_trading,
                "selected_account_id": settings.resolved_account_id or None,
                "message": (
                    "Demo execution enabled. Live trading remains locked."
                ),
            },
        )

    @app.post(
        "/api/execution/disable",
        dependencies=[Depends(require_admin_api)],
    )
    def api_execution_disable() -> JSONResponse:
        """Disable all execution (demo + live) and return to dry-run.

        Flips ENABLE_TOPSTEP_ORDER_EXECUTION, TOPSTEP_EXECUTION_CONFIRM,
        ENABLE_LIVE_TRADING, LIVE_TRADING_CONFIRM, and
        LIVE_TRADING_ACCOUNT_ACK back to their safe defaults. Broker
        provider + selected account stay untouched so the account
        connection survives.
        """
        try:
            execution_mode = settings_store.update_typed(
                "EXECUTION_MODE", "paper"
            )
            order_exec_flag = settings_store.update_typed(
                "ENABLE_TOPSTEP_ORDER_EXECUTION", "false"
            )
            confirm_token = settings_store.update_typed(
                "TOPSTEP_EXECUTION_CONFIRM", "disabled"
            )
            live_flag = settings_store.update_typed(
                "ENABLE_LIVE_TRADING", "false"
            )
            live_confirm = settings_store.update_typed(
                "LIVE_TRADING_CONFIRM", "disabled"
            )
            live_ack = settings_store.update_typed(
                "LIVE_TRADING_ACCOUNT_ACK", "false"
            )
        except SettingsValidationError as exc:
            return JSONResponse(
                status_code=400,
                content={
                    "ok": False,
                    "status": "settings_error",
                    "message": str(exc),
                },
            )
        for key, value in (
            ("EXECUTION_MODE", execution_mode),
            ("ENABLE_TOPSTEP_ORDER_EXECUTION", order_exec_flag),
            ("TOPSTEP_EXECUTION_CONFIRM", confirm_token),
            ("ENABLE_LIVE_TRADING", live_flag),
            ("LIVE_TRADING_CONFIRM", live_confirm),
            ("LIVE_TRADING_ACCOUNT_ACK", live_ack),
        ):
            settings_store.apply_to_settings(settings, key, value)
        if isinstance(broker, TopstepBroker):
            broker.execution_mode = execution_mode
            broker.enable_order_execution = order_exec_flag
            broker.execution_confirm = confirm_token
            broker.enable_live_trading = live_flag
            broker.live_trading_confirm = live_confirm
            broker.live_trading_account_ack = live_ack
        log.warning(
            "EXECUTION DISABLED: provider=%s mode=%s account=%s",
            settings.resolved_provider,
            settings.execution_mode,
            settings.resolved_account_id,
        )
        return JSONResponse(
            status_code=200,
            content={
                "ok": True,
                "status": "execution_disabled",
                "execution_mode": settings.execution_mode,
                "enable_topstep_order_execution": (
                    settings.enable_topstep_order_execution
                ),
                "topstep_execution_confirm": (
                    settings.topstep_execution_confirm
                ),
                "enable_live_trading": settings.enable_live_trading,
                "live_trading_confirm": settings.live_trading_confirm,
                "live_trading_account_ack": (
                    settings.live_trading_account_ack
                ),
                "message": (
                    "Execution disabled. App is back in safe dry-run mode."
                ),
            },
        )

    @app.post(
        "/api/topstep/smoke-test",
        dependencies=[Depends(require_admin_api)],
    )
    async def api_topstep_smoke_test(request: Request) -> JSONResponse:
        """Dual-mode smoke test of the Topstep order-routing pipeline.

        Body (all optional):
          * ``symbol`` — defaults to ``MES1!``.
          * ``contracts`` — defaults to 1.
          * ``execute`` — when ``true``, actually submit a small enter +
            exit pair via ``submit_market_order``. Requires every
            execution gate to be armed first, and ``confirmation`` to
            equal exactly ``smoke``. Default ``false`` (preview only).
          * ``confirmation`` — required when ``execute`` is true.

        Behaviour:
          * ``execute=false`` → builds BUY entry + SELL exit previews
            via ``build_order_preview``. Never calls
            ``/api/Order/place``. Returns ``would_submit=false``.
          * ``execute=true`` → calls ``submit_market_order`` twice
            (BUY entry, then SELL exit). Relies on the broker safety
            check for the demo/live gate stack; refuses cleanly if any
            gate is open. Returns ``would_submit=true`` plus both
            broker responses, and journals each action.
        """
        from .schemas import NormalizedSignal

        try:
            body = await request.json()
        except Exception:
            body = None
        body = body if isinstance(body, dict) else {}

        symbol = str(body.get("symbol") or "").strip() or "MES1!"
        try:
            contracts = int(body.get("contracts") or 1)
        except (TypeError, ValueError):
            contracts = 1
        if contracts < 1:
            contracts = 1
        execute = bool(body.get("execute"))
        confirmation = str(body.get("confirmation") or "").strip()

        checks: list[dict[str, Any]] = []

        provider_ok = settings.resolved_provider == "topstep"
        checks.append(
            {
                "name": "broker_provider",
                "ok": provider_ok,
                "detail": settings.resolved_provider,
            }
        )
        account_id = settings.resolved_account_id or ""
        account_ok = bool(account_id)
        checks.append(
            {
                "name": "selected_account",
                "ok": account_ok,
                "detail": account_id or None,
            }
        )
        broker_symbol = None
        if symbol_map is not None:
            try:
                broker_symbol = symbol_map.resolve_explicit(
                    symbol, "topstep"
                )
            except Exception:  # pragma: no cover - defensive
                broker_symbol = None
        mapping_ok = bool(broker_symbol)
        checks.append(
            {
                "name": "symbol_mapping",
                "ok": mapping_ok,
                "detail": broker_symbol or None,
            }
        )

        # ---- execute=false: preview-only path ----
        if not execute:
            entry_preview: Optional[dict[str, Any]] = None
            exit_preview: Optional[dict[str, Any]] = None
            if (
                provider_ok and account_ok and mapping_ok
                and isinstance(broker, TopstepBroker)
            ):
                entry_signal = NormalizedSignal(
                    source="smoke_test",
                    strategy="dashboard_smoke_test",
                    symbol=symbol,
                    broker_symbol=broker_symbol,
                    exchange=None,
                    action="BUY",
                    contracts=contracts,
                    price=None,
                    order_id=None,
                    comment="smoke_test_entry",
                    timeframe=None,
                    raw={},
                )
                try:
                    entry_preview = broker.build_order_preview(
                        entry_signal, symbol_map=symbol_map
                    )
                except Exception as exc:  # pragma: no cover - defensive
                    entry_preview = {"ok": False, "error": str(exc)}
                checks.append(
                    {
                        "name": "entry_preview_built",
                        "ok": bool(entry_preview)
                        and entry_preview.get("ok") is not False,
                        "detail": (
                            entry_preview.get("status")
                            if entry_preview
                            else None
                        ),
                    }
                )

                exit_signal = NormalizedSignal(
                    source="smoke_test",
                    strategy="dashboard_smoke_test",
                    symbol=symbol,
                    broker_symbol=broker_symbol,
                    exchange=None,
                    action="SELL",
                    contracts=contracts,
                    price=None,
                    order_id=None,
                    comment="smoke_test_exit",
                    timeframe=None,
                    raw={},
                )
                try:
                    exit_preview = broker.build_order_preview(
                        exit_signal, symbol_map=symbol_map
                    )
                except Exception as exc:  # pragma: no cover - defensive
                    exit_preview = {"ok": False, "error": str(exc)}
                checks.append(
                    {
                        "name": "exit_preview_built",
                        "ok": bool(exit_preview)
                        and exit_preview.get("ok") is not False,
                        "detail": (
                            exit_preview.get("status")
                            if exit_preview
                            else None
                        ),
                    }
                )

            ok = all(c["ok"] for c in checks)
            return JSONResponse(
                status_code=200,
                content={
                    "ok": ok,
                    "status": "smoke_test_ok" if ok else "smoke_test_failed",
                    "execute": False,
                    "symbol": symbol,
                    "broker_symbol": broker_symbol,
                    "account_id": account_id or None,
                    "contracts": contracts,
                    "execution_mode": settings.execution_mode,
                    "entry_preview": entry_preview,
                    "exit_preview": exit_preview,
                    "checks": checks,
                    "would_submit": False,
                    "message": (
                        "Dry-run smoke test passed — entry + exit "
                        "payloads built. No broker order was sent."
                        if ok
                        else "Dry-run smoke test failed — review checks "
                             "for the first blocking gate. No broker "
                             "order was sent."
                    ),
                },
            )

        # ---- execute=true: real submission path ----
        if confirmation != "smoke":
            return JSONResponse(
                status_code=400,
                content={
                    "ok": False,
                    "status": "invalid_confirmation",
                    "execute": True,
                    "would_submit": False,
                    "message": (
                        "confirmation must equal 'smoke' to run the "
                        "live smoke test"
                    ),
                },
            )

        if not provider_ok:
            return JSONResponse(
                status_code=400,
                content={
                    "ok": False,
                    "status": "broker_provider_not_topstep",
                    "execute": True,
                    "would_submit": False,
                    "checks": checks,
                    "message": (
                        f"BROKER_PROVIDER is {settings.resolved_provider!r}"
                        " — set it to 'topstep' before running the live"
                        " smoke test"
                    ),
                },
            )
        if not account_ok:
            return JSONResponse(
                status_code=400,
                content={
                    "ok": False,
                    "status": "no_selected_account",
                    "execute": True,
                    "would_submit": False,
                    "checks": checks,
                    "message": (
                        "no Topstep account selected — set "
                        "SELECTED_ACCOUNT_ID / TOPSTEP_ACCOUNT_ID first"
                    ),
                },
            )
        if not mapping_ok:
            return JSONResponse(
                status_code=400,
                content={
                    "ok": False,
                    "status": "symbol_mapping_missing",
                    "execute": True,
                    "would_submit": False,
                    "checks": checks,
                    "message": (
                        f"no Topstep contract id for {symbol!r} — add it "
                        "in Configuration > Symbols"
                    ),
                },
            )
        if kill_switch.is_active():
            return JSONResponse(
                status_code=400,
                content={
                    "ok": False,
                    "status": "kill_switch_active",
                    "execute": True,
                    "would_submit": False,
                    "checks": checks,
                    "message": (
                        "kill switch is active — deactivate it before "
                        "running the live smoke test"
                    ),
                },
            )
        if contracts > max(1, settings.max_contracts_per_trade):
            return JSONResponse(
                status_code=400,
                content={
                    "ok": False,
                    "status": "contracts_above_max",
                    "execute": True,
                    "would_submit": False,
                    "checks": checks,
                    "message": (
                        f"contracts={contracts} exceeds "
                        f"MAX_CONTRACTS_PER_TRADE="
                        f"{settings.max_contracts_per_trade}"
                    ),
                },
            )
        # Require execution to already be armed (demo or live). The
        # broker safety check below will catch this too, but failing
        # here gives a cleaner gate label.
        if not settings.enable_topstep_order_execution:
            return JSONResponse(
                status_code=400,
                content={
                    "ok": False,
                    "status": "execution_not_armed",
                    "execute": True,
                    "would_submit": False,
                    "checks": checks,
                    "message": (
                        "execution is not armed — apply demo/live first"
                        " before running the live smoke test"
                    ),
                },
            )
        if not isinstance(broker, TopstepBroker):
            return JSONResponse(
                status_code=400,
                content={
                    "ok": False,
                    "status": "broker_provider_not_topstep",
                    "execute": True,
                    "would_submit": False,
                    "checks": checks,
                    "message": (
                        f"active broker is {broker.provider!r} — restart"
                        " with BROKER_PROVIDER=topstep to run the live"
                        " smoke test"
                    ),
                },
            )

        # Build signals + submit. Rely on submit_market_order for the
        # demo/live gate stack — it returns a structured refusal envelope
        # if any gate is open.
        entry_signal = NormalizedSignal(
            source="smoke_test",
            strategy="dashboard_smoke_test_entry",
            symbol=symbol,
            broker_symbol=broker_symbol,
            exchange=None,
            action="BUY",
            contracts=contracts,
            price=None,
            order_id="smoke_test_entry",
            comment="smoke_test entry",
            timeframe=None,
            raw={"smoke_test": True},
        )
        exit_signal = NormalizedSignal(
            source="smoke_test",
            strategy="dashboard_smoke_test_exit",
            symbol=symbol,
            broker_symbol=broker_symbol,
            exchange=None,
            action="SELL",
            contracts=contracts,
            price=None,
            order_id="smoke_test_exit",
            comment="smoke_test exit",
            timeframe=None,
            raw={"smoke_test": True},
        )

        entry_response = broker.submit_market_order(
            entry_signal, symbol_map=symbol_map
        )
        # If the entry failed, do not submit the exit — leave whatever
        # state the broker is in. The response makes the refusal clear.
        entry_ok = bool(entry_response.get("accepted")) and bool(
            entry_response.get("ok")
        )

        # Journal the entry attempt either way for audit.
        try:
            journal.record_signal(
                source="admin",
                strategy="smoke_test_entry",
                symbol=symbol,
                action="BUY",
                contracts=contracts,
                price=None,
                order_id=entry_response.get("broker_order_id")
                or entry_response.get("order_id"),
                raw_payload={
                    "event": "smoke_test_entry",
                    "symbol": symbol,
                    "broker_symbol": broker_symbol,
                    "contracts": contracts,
                    "execution_mode": settings.execution_mode,
                },
                decision="accepted" if entry_ok else "rejected",
                rejection_reason=(
                    None if entry_ok
                    else str(entry_response.get("status") or "submit_failed")
                ),
                execution_mode=settings.execution_mode,
                execution_result=entry_response,
                broker_provider=settings.resolved_provider,
                broker_symbol=broker_symbol,
                timeframe=None,
            )
        except Exception:  # pragma: no cover - audit only
            pass

        exit_response: dict[str, Any]
        if not entry_ok:
            exit_response = {
                "ok": False,
                "accepted": False,
                "status": "entry_failed_skipping_exit",
                "would_submit": False,
                "message": (
                    "entry submission failed — exit not attempted"
                ),
            }
        else:
            exit_response = broker.submit_market_order(
                exit_signal, symbol_map=symbol_map
            )
            try:
                journal.record_signal(
                    source="admin",
                    strategy="smoke_test_exit",
                    symbol=symbol,
                    action="SELL",
                    contracts=contracts,
                    price=None,
                    order_id=exit_response.get("broker_order_id")
                    or exit_response.get("order_id"),
                    raw_payload={
                        "event": "smoke_test_exit",
                        "symbol": symbol,
                        "broker_symbol": broker_symbol,
                        "contracts": contracts,
                        "execution_mode": settings.execution_mode,
                    },
                    decision=(
                        "accepted"
                        if exit_response.get("accepted") and exit_response.get("ok")
                        else "rejected"
                    ),
                    rejection_reason=(
                        None
                        if exit_response.get("accepted") and exit_response.get("ok")
                        else str(
                            exit_response.get("status") or "submit_failed"
                        )
                    ),
                    execution_mode=settings.execution_mode,
                    execution_result=exit_response,
                    broker_provider=settings.resolved_provider,
                    broker_symbol=broker_symbol,
                    timeframe=None,
                )
            except Exception:  # pragma: no cover - audit only
                pass

        exit_ok = bool(exit_response.get("accepted")) and bool(
            exit_response.get("ok")
        )

        log.warning(
            "TOPSTEP SMOKE TEST EXECUTED: provider=%s mode=%s account=%s "
            "symbol=%s contracts=%s entry_ok=%s exit_ok=%s",
            settings.resolved_provider,
            settings.execution_mode,
            settings.resolved_account_id,
            symbol,
            contracts,
            entry_ok,
            exit_ok,
        )

        overall_ok = entry_ok and exit_ok
        return JSONResponse(
            status_code=200,
            content={
                "ok": overall_ok,
                "status": (
                    "smoke_test_executed"
                    if overall_ok
                    else "smoke_test_partial"
                ),
                "execute": True,
                "would_submit": True,
                "symbol": symbol,
                "broker_symbol": broker_symbol,
                "account_id": account_id or None,
                "contracts": contracts,
                "execution_mode": settings.execution_mode,
                "entry_response": entry_response,
                "exit_response": exit_response,
                "checks": checks,
                "message": (
                    "Smoke test executed — entry + exit submitted."
                    if overall_ok
                    else "Smoke test partial — see entry_response / "
                         "exit_response for the failing leg."
                ),
            },
        )

    @app.post(
        "/api/topstep/submit-live-test-order",
        dependencies=[Depends(require_admin_api)],
    )
    async def api_topstep_submit_live_test_order(
        request: Request,
    ) -> JSONResponse:
        """Submit a tiny LIVE Topstep order against the funded account.

        Every live gate must be satisfied — the endpoint is unavailable
        unless ``/api/topstep/live-execution/enable`` has been called
        first. Refusals come back as ``live_execution_locked`` with the
        specific failing gate label. ``submit_market_order`` is reused
        so behavior matches the webhook path.
        """
        from .schemas import NormalizedSignal

        if broker.provider != "topstep":
            return JSONResponse(
                status_code=200,
                content={
                    "ok": False,
                    "status": "broker_provider_not_topstep",
                    "message": (
                        f"active provider is {broker.provider} — switch "
                        "BROKER_PROVIDER=topstep and restart"
                    ),
                    "would_submit": False,
                },
            )

        if settings.execution_mode != "live":
            return JSONResponse(
                status_code=200,
                content={
                    "ok": False,
                    "status": "live_execution_locked",
                    "gate": "execution_mode_not_live",
                    "message": (
                        "EXECUTION_MODE is not 'live' — arm live execution "
                        "before using this endpoint"
                    ),
                    "would_submit": False,
                },
            )

        try:
            body = await request.json()
        except Exception:
            body = None
        body = body if isinstance(body, dict) else {}

        symbol = (
            str(body.get("symbol") or "").strip()
            or (
                settings.live_allowed_symbols[0]
                if settings.live_allowed_symbols
                else ""
            )
        )
        if not symbol:
            return JSONResponse(
                status_code=400,
                content={
                    "ok": False,
                    "status": "missing_symbol",
                    "message": "symbol is required",
                    "would_submit": False,
                },
            )

        action_raw = str(body.get("action") or "").strip().lower()
        if action_raw in {"buy", "long"}:
            action = "BUY"
        elif action_raw in {"sell", "short"}:
            action = "SELL"
        else:
            return JSONResponse(
                status_code=400,
                content={
                    "ok": False,
                    "status": "unsupported_action",
                    "message": (
                        f"action must be 'buy' or 'sell' (got {action_raw!r})"
                    ),
                    "would_submit": False,
                },
            )

        try:
            contracts = int(body.get("contracts") or 1)
        except (TypeError, ValueError):
            return JSONResponse(
                status_code=400,
                content={
                    "ok": False,
                    "status": "invalid_contracts",
                    "message": "contracts must be a positive integer",
                    "would_submit": False,
                },
            )
        if contracts < 1:
            return JSONResponse(
                status_code=400,
                content={
                    "ok": False,
                    "status": "invalid_contracts",
                    "message": "contracts must be >= 1",
                    "would_submit": False,
                },
            )
        if contracts > settings.live_max_contracts_per_trade:
            return JSONResponse(
                status_code=400,
                content={
                    "ok": False,
                    "status": "live_contracts_above_max",
                    "message": (
                        f"contracts={contracts} exceeds "
                        f"LIVE_MAX_CONTRACTS_PER_TRADE="
                        f"{settings.live_max_contracts_per_trade}"
                    ),
                    "would_submit": False,
                },
            )
        if contracts > settings.max_contracts_per_trade:
            return JSONResponse(
                status_code=400,
                content={
                    "ok": False,
                    "status": "contracts_above_max",
                    "message": (
                        f"contracts={contracts} exceeds "
                        f"MAX_CONTRACTS_PER_TRADE="
                        f"{settings.max_contracts_per_trade}"
                    ),
                    "would_submit": False,
                },
            )

        allowed = [s.strip() for s in settings.live_allowed_symbols if s and s.strip()]
        if symbol not in allowed:
            return JSONResponse(
                status_code=400,
                content={
                    "ok": False,
                    "status": "live_symbol_not_allowed",
                    "message": (
                        f"symbol {symbol!r} is not in LIVE_ALLOWED_SYMBOLS"
                    ),
                    "would_submit": False,
                },
            )

        explicit_mapping = (
            symbol_map.resolve_explicit(symbol, broker.provider)
            if symbol_map is not None
            else None
        )
        if not explicit_mapping:
            return JSONResponse(
                status_code=400,
                content={
                    "ok": False,
                    "status": "symbol_mapping_missing",
                    "message": (
                        f"Topstep contract id missing for {symbol!r} —"
                        " add it in Configuration > Symbols"
                    ),
                    "would_submit": False,
                },
            )
        broker_symbol = explicit_mapping
        signal = NormalizedSignal(
            source="manual_test",
            strategy="topstep_submit_live_test_order",
            symbol=symbol,
            broker_symbol=broker_symbol,
            exchange=None,
            action=action,
            contracts=contracts,
            price=None,
            order_id=body.get("order_id"),
            comment="topstep_submit_live_test_order",
            timeframe=None,
            raw=body,
        )

        topstep = _topstep_adapter_for_admin()
        # Make sure the adapter sees the current live-trading runtime
        # state before evaluating gates.
        topstep.execution_mode = settings.execution_mode
        topstep.enable_order_execution = (
            settings.enable_topstep_order_execution
        )
        topstep.execution_confirm = settings.topstep_execution_confirm
        topstep.enable_live_trading = settings.enable_live_trading
        topstep.live_trading_confirm = settings.live_trading_confirm
        topstep.live_trading_account_ack = (
            settings.live_trading_account_ack
        )
        topstep.live_max_contracts_per_trade = (
            settings.live_max_contracts_per_trade
        )
        topstep.live_allowed_symbols = list(
            settings.live_allowed_symbols
        )
        topstep.live_require_kill_switch_off = (
            settings.live_require_kill_switch_off
        )
        topstep.max_contracts_per_trade = settings.max_contracts_per_trade
        topstep.kill_switch_active = kill_switch.is_active()

        result = topstep.submit_market_order(signal, symbol_map=symbol_map)
        return JSONResponse(status_code=200, content=result)

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
        "/api/broker/positions/reconcile",
        dependencies=[Depends(require_admin_api)],
    )
    def api_broker_positions_reconcile() -> JSONResponse:
        """Report differences between broker and journal positions.

        Read-only: never auto-corrects. The operator decides what to do
        when the two disagree (typical causes: manual close in the
        broker UI, broker EOD auto-flatten, or — for the Topstep
        adapter today — the journal never wrote a position row for a
        topstep submission).

        Dedupe is by the raw symbol key, so a TradingView ticker in
        the journal (``MES1!``) and a ProjectX contract id from the
        broker (``CON.F.US.MES.M26``) appear in separate buckets even
        when they refer to the same instrument. That's intentional —
        the goal is to surface mismatches for human review, not
        silently reconcile them.
        """
        broker_resp: dict[str, Any] = _safe_broker_call("get_positions")
        journal_rows = journal.list_open_positions()

        # Build symbol → qty maps for both sides. Quantity uses ``size``
        # for the broker side (ProjectX schema) with fallbacks for
        # other shapes; the journal uses ``quantity``.
        broker_positions: list[dict[str, Any]] = []
        broker_qty: dict[str, int] = {}
        if (
            broker_resp.get("ok") is True
            and isinstance(broker_resp.get("positions"), list)
        ):
            for pos in broker_resp["positions"]:
                if not isinstance(pos, dict):
                    continue
                key = ""
                for k in ("symbol", "contractId", "contract_id"):
                    if pos.get(k):
                        key = str(pos[k])
                        break
                if not key:
                    continue
                qty_raw = (
                    pos.get("quantity")
                    if pos.get("quantity") is not None
                    else pos.get("size")
                )
                try:
                    qty = int(qty_raw) if qty_raw is not None else 0
                except (TypeError, ValueError):
                    qty = 0
                if qty == 0:
                    continue
                broker_positions.append({"symbol": key, "quantity": qty})
                broker_qty[key] = qty

        journal_positions: list[dict[str, Any]] = []
        journal_qty: dict[str, int] = {}
        for row in journal_rows:
            sym = row.get("symbol")
            qty = int(row.get("quantity") or 0)
            if not sym or qty == 0:
                continue
            journal_positions.append({"symbol": str(sym), "quantity": qty})
            journal_qty[str(sym)] = qty

        differences: list[dict[str, Any]] = []
        for sym in sorted(set(broker_qty) | set(journal_qty)):
            b = broker_qty.get(sym)
            j = journal_qty.get(sym)
            if b is None:
                differences.append(
                    {
                        "symbol": sym,
                        "kind": "not_in_broker",
                        "journal_quantity": j,
                    }
                )
            elif j is None:
                differences.append(
                    {
                        "symbol": sym,
                        "kind": "not_in_journal",
                        "broker_quantity": b,
                    }
                )
            elif b != j:
                differences.append(
                    {
                        "symbol": sym,
                        "kind": "qty_mismatch",
                        "broker_quantity": b,
                        "journal_quantity": j,
                    }
                )

        return JSONResponse(
            status_code=200,
            content={
                "ok": True,
                "provider": broker.provider,
                "broker_reachable": bool(broker_resp.get("ok")),
                "broker_status": broker_resp.get("status"),
                "broker_positions": broker_positions,
                "journal_positions": journal_positions,
                "differences": differences,
                "in_sync": not differences,
            },
        )

    @app.get(
        "/api/broker/orders", dependencies=[Depends(require_admin_api)]
    )
    def api_broker_orders() -> dict[str, Any]:
        return _safe_broker_call("get_orders")

    @app.get(
        "/api/broker/order-history",
        dependencies=[Depends(require_admin_api)],
    )
    def api_broker_order_history(
        lookback_days: Optional[int] = None,
        limit: Optional[int] = None,
    ) -> JSONResponse:
        """Recent broker order history, normalized for the dashboard.

        Defaults to ``ORDER_HISTORY_LOOKBACK_DAYS`` / ``ORDER_HISTORY_LIMIT``
        when the caller does not specify a window. Returns a stable
        envelope; never raises so the UI can render a clean empty/error
        state when ProjectX is unreachable.
        """
        days = lookback_days or settings.order_history_lookback_days
        rows = limit or settings.order_history_limit
        if broker.provider != "topstep":
            return JSONResponse(
                status_code=200,
                content={
                    "ok": False,
                    "provider": broker.provider,
                    "status": "not_implemented_for_provider",
                    "message": (
                        f"order history not available for the "
                        f"{broker.provider} provider"
                    ),
                    "lookback_days": days,
                    "limit": rows,
                    "orders": [],
                    "count": 0,
                    "not_implemented": True,
                },
            )
        topstep = _topstep_adapter_for_admin()
        try:
            result = topstep.get_order_history(
                lookback_days=days, limit=rows
            )
        except Exception as exc:  # pragma: no cover - defensive
            return JSONResponse(
                status_code=200,
                content={
                    "ok": False,
                    "provider": broker.provider,
                    "status": "error",
                    "message": (
                        f"order history failed: {exc.__class__.__name__}"
                    ),
                    "lookback_days": days,
                    "limit": rows,
                    "orders": [],
                    "count": 0,
                },
            )
        return JSONResponse(status_code=200, content=result)

    @app.get(
        "/api/realtime/state", dependencies=[Depends(require_admin_api)]
    )
    def api_realtime_state() -> JSONResponse:
        """Snapshot used by the auto-refreshing dashboard panels.

        Bundles broker positions + open orders + a Last-refreshed
        timestamp in one call so the JS only fires one request per
        polling cycle. Returns 200 with a structured envelope even
        when the broker call fails.
        """
        from datetime import datetime, timezone

        positions = _safe_broker_call("get_positions")
        orders = _safe_broker_call("get_orders")
        return JSONResponse(
            status_code=200,
            content={
                "ok": True,
                "provider": broker.provider,
                "broker_provider": settings.resolved_provider,
                "selected_account_id": settings.resolved_account_id or None,
                "refreshed_at": datetime.now(timezone.utc).isoformat(),
                "realtime_enabled": settings.enable_topstep_realtime,
                "realtime_mode": settings.topstep_realtime_mode,
                "realtime_poll_seconds": (
                    settings.topstep_realtime_poll_seconds
                ),
                "positions": {
                    "ok": bool(positions.get("ok")),
                    "status": positions.get("status", "unknown"),
                    "message": positions.get("message", ""),
                    "not_implemented": bool(positions.get("not_implemented")),
                    "rows": positions.get("positions") or [],
                    "count": len(positions.get("positions") or []),
                },
                "orders": {
                    "ok": bool(orders.get("ok")),
                    "status": orders.get("status", "unknown"),
                    "message": orders.get("message", ""),
                    "not_implemented": bool(orders.get("not_implemented")),
                    "rows": orders.get("orders") or [],
                    "count": len(orders.get("orders") or []),
                },
            },
        )

    @app.get("/api/system", dependencies=[Depends(require_admin_api)])
    def api_system() -> dict[str, Any]:
        return system_summary(
            settings=settings, broker=broker, kill_switch=kill_switch
        )

    # ------------------------------------------------------------------
    # Paper-only admin actions (flatten / reset)
    #
    # These only operate on the in-memory paper broker. If the active
    # provider is topstep they return a structured, safe
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

    @app.post(
        "/api/broker/flatten-all",
        dependencies=[Depends(require_admin_api)],
    )
    def api_broker_flatten_all() -> JSONResponse:
        """Provider-aware flatten / exit-all.

        Paper: flattens all in-memory positions via the existing paper
        flatten path. Topstep: routes through the real
        ``flatten_position()`` which closes each open position via
        ``/api/Position/closeContract`` and reports a per-leg envelope.
        Other providers: structured not-implemented envelope.
        """
        if broker.provider == "paper":
            return JSONResponse(
                status_code=200, content=broker.flatten_all_positions()
            )
        if broker.provider == "topstep":
            return JSONResponse(
                status_code=200, content=broker.flatten_position()
            )
        return JSONResponse(
            status_code=200,
            content={
                "ok": False,
                "provider": broker.provider,
                "status": "not_implemented",
                "not_implemented": True,
                "message": (
                    f"{broker.provider} flatten-all is not implemented yet"
                ),
            },
        )

    # M5 — per-process token bucket guarding the webhook. A misconfigured
    # TradingView alert template (or anyone with the secret) firing
    # tightly could otherwise saturate the broker / daily-loss limit.
    webhook_rate_limiter = TokenBucket(
        rate_per_second=settings.webhook_rate_limit_per_second,
        burst=settings.webhook_rate_burst,
    )
    app.state.webhook_rate_limiter = webhook_rate_limiter

    @app.post("/webhooks/tradingview")
    async def tradingview_webhook(request: Request):
        if not webhook_rate_limiter.allow():
            log.warning(
                "webhook rate-limited (limit=%s/s burst=%s)",
                settings.webhook_rate_limit_per_second,
                settings.webhook_rate_burst,
            )
            # Journal the refusal so the operator can see it. We don't
            # have a parsed payload yet — stub the row with what we know.
            journal.record_signal(
                source=None,
                strategy=None,
                symbol=None,
                action=None,
                contracts=None,
                price=None,
                order_id=None,
                raw_payload={"_rate_limited": True},
                decision="rejected",
                rejection_reason="rate_limited",
                execution_mode=broker.execution_mode,
                execution_result=None,
                broker_provider=broker.provider,
                broker_symbol=None,
                timeframe=None,
            )
            return JSONResponse(
                status_code=429,
                content={
                    "accepted": False,
                    "decision": "rejected",
                    "rejection_reason": "rate_limited",
                    "message": (
                        "webhook rate limit exceeded — slow down or raise "
                        "WEBHOOK_RATE_LIMIT_PER_SECOND / WEBHOOK_RATE_BURST"
                    ),
                },
            )

        try:
            payload = await request.json()
        except Exception:
            payload = None
        # Xiznit native alerts can't carry our secret in the body —
        # accept it from the query string or X-SignalBridge-Secret
        # header. Body wins when present (handled inside ``handle``).
        query_secret = request.query_params.get("secret")
        header_secret = request.headers.get("x-signalbridge-secret")
        request_secret = header_secret or query_secret
        query_symbol = request.query_params.get("symbol")
        return handler.handle(
            payload,
            request_secret=request_secret,
            query_symbol=query_symbol,
        )

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
        broker_snapshot = ctx.get("broker_status") or broker_status_payload(
            settings=settings, broker=broker
        )
        demo_exec = _demo_execution_view(
            settings=settings,
            broker_snapshot=broker_snapshot,
            kill_switch=kill_switch,
        )
        live_exec = _live_execution_view(
            settings=settings,
            broker_snapshot=broker_snapshot,
            kill_switch=kill_switch,
        )
        # Derive the execution-card state. Drives the CSS class on
        # the card and the headline status pill label.
        if kill_switch.is_active():
            exec_card_state = "kill-switch-active"
            exec_card_label = "Kill Switch Active"
        elif live_exec["is_armed"]:
            exec_card_state = "live-armed"
            exec_card_label = "Live Armed"
        elif settings.execution_mode == "live":
            # Live selected but not engaged — locked until the engagement
            # flow runs.
            exec_card_state = "live-locked"
            exec_card_label = "Live Locked"
        elif settings.execution_mode == "demo":
            exec_card_state = "demo"
            exec_card_label = "Demo"
        elif settings.execution_mode == "paper":
            exec_card_state = "dry-run"
            exec_card_label = "Dry Run"
        else:
            exec_card_state = "disabled"
            exec_card_label = "Disabled"

        # Ticker Watch placeholder: list a few mapped tickers from the
        # symbol map so the operator can pick one. Resolution happens
        # client-side; no live market feed is wired in this build.
        ticker_options: list[dict[str, Any]] = []
        try:
            mappings = symbol_map.all_mappings()
        except Exception:  # pragma: no cover - defensive
            mappings = {}
        for ticker, providers in (mappings or {}).items():
            if not isinstance(providers, dict):
                continue
            ticker_options.append(
                {
                    "ticker": ticker,
                    "contract_id": providers.get(broker.provider)
                    or providers.get("topstep")
                    or "",
                }
            )
        ticker_options.sort(key=lambda r: r["ticker"])
        ticker_watch = {
            "tickers": ticker_options,
            "default_ticker": ticker_options[0]["ticker"] if ticker_options else "",
            "default_contract_id": (
                ticker_options[0]["contract_id"] if ticker_options else ""
            ),
            "mode_label": "polling / SignalR not enabled",
            "feed_note": (
                "Realtime price feed will use ProjectX market hub later."
            ),
        }

        ctx.update(
            {
                "demo_execution": demo_exec,
                "live_execution": live_exec,
                "exec_card_state": exec_card_state,
                "exec_card_label": exec_card_label,
                # Dashboard dropdown intentionally omits "demo" — Topstep
                # does not expose a freely-controllable paper/demo
                # surface, and dry-run + live cover every real flow.
                # The backend still accepts demo for sbctl / scripting
                # paths.
                "execution_mode_options": ["paper", "live"],
                "ticker_watch": ticker_watch,
            }
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
        demo_exec = _demo_execution_view(
            settings=settings,
            broker_snapshot=broker_snapshot,
            kill_switch=kill_switch,
        )
        live_exec = _live_execution_view(
            settings=settings,
            broker_snapshot=broker_snapshot,
            kill_switch=kill_switch,
        )
        realtime_view = {
            "enabled": settings.enable_topstep_realtime,
            "mode": settings.topstep_realtime_mode,
            "poll_seconds": settings.topstep_realtime_poll_seconds,
            "label": (
                f"Polling every {settings.topstep_realtime_poll_seconds}s"
                if settings.topstep_realtime_mode == "polling"
                else "SignalR (placeholder)"
            ),
            "signalr_enabled": False,
        }
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
                    "base_url": settings.topstep_base_url,
                    "ws_url": settings.topstep_ws_url,
                    "token_cached": bool(settings.topstep_token),
                    "token_expires_at": settings.topstep_token_expires_at or "",
                },
                "demo_execution": demo_exec,
                "live_execution": live_exec,
                "realtime": realtime_view,
                "provider_options": ["paper", "topstep"],
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
        topstep_env: str = Form("demo"),
        topstep_base_url: str = Form("https://api.topstepx.com"),
        topstep_ws_url: str = Form("https://rtc.topstepx.com"),
    ):
        # The form's TOPSTEP_ACCOUNT_ID input was removed in the polish
        # pass — the dropdown owns the selection. For the topstep
        # provider we keep TOPSTEP_ACCOUNT_ID in sync with the dropdown
        # value so anything reading the env var on the next boot picks
        # up the same account.
        updates: list[tuple[str, Any]] = [
            ("BROKER_PROVIDER", broker_provider),
            ("EXECUTION_MODE", execution_mode),
            ("SELECTED_ACCOUNT_ID", selected_account_id),
            ("TOPSTEP_USERNAME", topstep_username),
            ("TOPSTEP_ENV", topstep_env),
            ("TOPSTEP_BASE_URL", topstep_base_url),
            ("TOPSTEP_WS_URL", topstep_ws_url),
        ]
        if str(broker_provider or "").strip().lower() == "topstep":
            updates.append(("TOPSTEP_ACCOUNT_ID", selected_account_id))
        if topstep_api_key != _TOPSTEP_API_KEY_UNCHANGED:
            updates.append(("TOPSTEP_API_KEY", topstep_api_key))

        # The broker form is intentionally not a path to live execution —
        # arming live trading requires the dedicated /api/topstep/
        # live-execution/enable endpoint with its full confirmation flow.
        if str(execution_mode or "").strip().lower() == "live":
            return _flash_redirect(
                "/settings/broker",
                "EXECUTION_MODE=live cannot be set from this form — use "
                "the Live Execution panel below to arm live trading.",
                kind="error",
            )

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
        "/settings/profile",
        response_class=HTMLResponse,
        dependencies=[Depends(require_admin_page)],
    )
    def page_settings_profile(request: Request) -> HTMLResponse:
        ctx = _page_ctx(request)
        stored_hash = settings.admin_password_hash or ""
        ctx.update(
            {
                "profile": {
                    "username": settings.admin_username or "",
                    "password_uses_hash": bool(stored_hash),
                    "password_min_length": 10,
                }
            }
        )
        return templates.TemplateResponse(
            request, "settings_profile.html", ctx
        )

    @app.post(
        "/settings/profile",
        dependencies=[Depends(require_admin_page)],
    )
    def post_settings_profile(
        current_password: str = Form(""),
        new_username: str = Form(""),
        new_password: str = Form(""),
        confirm_password: str = Form(""),
    ):
        new_username = (new_username or "").strip()
        new_password = new_password or ""
        confirm_password = confirm_password or ""
        # Always require the current password before changing anything —
        # session-only is not enough since a hijacked tab could otherwise
        # rotate credentials without proving knowledge of the current one.
        if not check_credentials(
            settings, settings.admin_username, current_password
        ):
            return _flash_redirect(
                "/settings/profile",
                "Current password is incorrect.",
                kind="error",
            )

        if not new_username:
            return _flash_redirect(
                "/settings/profile",
                "Username cannot be empty.",
                kind="error",
            )

        changing_password = bool(new_password or confirm_password)
        if changing_password:
            if new_password != confirm_password:
                return _flash_redirect(
                    "/settings/profile",
                    "New password and confirmation do not match.",
                    kind="error",
                )
            if len(new_password) < 10:
                return _flash_redirect(
                    "/settings/profile",
                    "New password must be at least 10 characters.",
                    kind="error",
                )

        try:
            username_value = settings_store.update_typed(
                "ADMIN_USERNAME", new_username
            )
        except SettingsValidationError as exc:
            return _flash_redirect(
                "/settings/profile", str(exc), kind="error"
            )
        settings_store.apply_to_settings(
            settings, "ADMIN_USERNAME", username_value
        )

        if changing_password:
            try:
                hashed = hash_password(new_password)
                stored_hash = settings_store.update_typed(
                    "ADMIN_PASSWORD_HASH", hashed
                )
            except (SettingsValidationError, ValueError) as exc:
                return _flash_redirect(
                    "/settings/profile", str(exc), kind="error"
                )
            settings_store.apply_to_settings(
                settings, "ADMIN_PASSWORD_HASH", stored_hash
            )
            # The plaintext fallback is now stale — clear the in-memory
            # value so a future check_credentials never accepts it again.
            settings.admin_password = ""
            log.info("admin profile updated (password changed)")
        else:
            log.info("admin profile updated (username only)")

        return _flash_redirect(
            "/settings/profile",
            "Profile updated.",
            kind="ok",
        )

    @app.get(
        "/settings/risk",
        response_class=HTMLResponse,
        dependencies=[Depends(require_admin_page)],
    )
    def page_settings_risk(request: Request) -> HTMLResponse:
        ctx = _page_ctx(request)
        ctx["risk"] = {
            "max_contracts_per_trade": settings.max_contracts_per_trade,
            "strategy_managed_risk": settings.strategy_managed_risk,
            "fixed_contracts_per_trade": settings.fixed_contracts_per_trade,
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
        max_contracts_per_trade: str = Form(...),
        strategy_managed_risk: str = Form("false"),
        fixed_contracts_per_trade: str = Form("1"),
        max_daily_loss: str = Form(...),
        max_open_positions: str = Form(...),
        duplicate_order_cooldown_seconds: str = Form(...),
        enable_longs: str = Form("false"),
        enable_shorts: str = Form("false"),
        enable_timeframe_lock: str = Form("false"),
        allowed_timeframes: str = Form(""),
        # Allowed symbols is no longer surfaced on the risk page. The
        # backend setting still exists (advanced/system settings will
        # own it later). Accept the field optionally so legacy clients +
        # tests can still update it.
        allowed_symbols: Optional[str] = Form(None),
    ):
        # Coerce + validate every field individually first so a bad input
        # surfaces a typed error before we touch SQLite.
        raw_updates: list[tuple[str, Any]] = [
            ("MAX_CONTRACTS_PER_TRADE", max_contracts_per_trade),
            ("STRATEGY_MANAGED_RISK", strategy_managed_risk),
            ("FIXED_CONTRACTS_PER_TRADE", fixed_contracts_per_trade),
            ("MAX_DAILY_LOSS", max_daily_loss),
            ("MAX_OPEN_POSITIONS", max_open_positions),
            ("DUPLICATE_ORDER_COOLDOWN_SECONDS", duplicate_order_cooldown_seconds),
            ("ENABLE_LONGS", enable_longs),
            ("ENABLE_SHORTS", enable_shorts),
            ("ENABLE_TIMEFRAME_LOCK", enable_timeframe_lock),
            ("ALLOWED_TIMEFRAMES", allowed_timeframes),
        ]
        if allowed_symbols is not None:
            raw_updates.append(("ALLOWED_SYMBOLS", allowed_symbols))
        from .settings_store import coerce as _coerce_key, serialize as _serialize_key

        try:
            coerced: dict[str, Any] = {}
            for key, value in raw_updates:
                coerced[key] = _coerce_key(key, value)
        except SettingsValidationError as exc:
            return _flash_redirect("/settings/risk", str(exc), kind="error")

        # Cross-field: FIXED_CONTRACTS_PER_TRADE must not exceed
        # MAX_CONTRACTS_PER_TRADE. Otherwise a non-strategy-managed run
        # could only ever produce a "contracts_above_max" rejection.
        if (
            coerced["FIXED_CONTRACTS_PER_TRADE"]
            > coerced["MAX_CONTRACTS_PER_TRADE"]
        ):
            return _flash_redirect(
                "/settings/risk",
                "FIXED_CONTRACTS_PER_TRADE cannot exceed MAX_CONTRACTS_PER_TRADE",
                kind="error",
            )

        for key, value in coerced.items():
            settings_store.set_setting(key, _serialize_key(key, value))
            settings_store.apply_to_settings(settings, key, value)
        return _flash_redirect(
            "/settings/risk", "Risk settings saved.", kind="ok"
        )

    @app.get(
        "/settings/symbols",
        response_class=HTMLResponse,
        dependencies=[Depends(require_admin_page)],
    )
    def page_settings_symbols(request: Request) -> HTMLResponse:
        ctx = _page_ctx(request)
        ctx.update(
            {
                "mappings": symbol_map.all_mappings(),
                "symbols_path": str(settings.symbols_map_abs_path),
            }
        )
        return templates.TemplateResponse(
            request, "settings_symbols.html", ctx
        )

    @app.post(
        "/settings/symbols",
        dependencies=[Depends(require_admin_page)],
    )
    async def post_settings_symbols(request: Request):
        form = await request.form()
        tickers = form.getlist("ticker")
        papers = form.getlist("paper")
        topsteps = form.getlist("topstep")
        try:
            mappings = parse_form_mappings(tickers, papers, topsteps)
        except ValueError as exc:
            return _flash_redirect("/settings/symbols", str(exc), kind="error")
        try:
            symbol_map.replace_all(mappings)
        except OSError as exc:
            return _flash_redirect(
                "/settings/symbols",
                f"could not write {settings.symbols_map_abs_path}: {exc}",
                kind="error",
            )
        msg = f"{len(mappings)} symbol mapping(s) saved."
        return _flash_redirect("/settings/symbols", msg, kind="ok")

    @app.post(
        "/api/topstep/contracts/search",
        dependencies=[Depends(require_admin_api)],
    )
    async def api_topstep_contracts_search(
        request: Request,
    ) -> JSONResponse:
        """Proxy to Topstep ProjectX ``POST /api/Contract/search``.

        Body: ``{"searchText": str, "live": bool}``. Returns the
        normalized list of contracts (id / name / description / tickSize /
        tickValue / activeContract / symbolId) so the Symbols page can
        render a results table.
        """
        try:
            body = await request.json()
        except Exception:
            body = None
        if not isinstance(body, dict):
            body = {}
        search_text = str(body.get("searchText") or "").strip()
        live_flag = bool(body.get("live", False))
        if not search_text:
            return JSONResponse(
                status_code=200,
                content={
                    "ok": False,
                    "status": "missing_search_text",
                    "message": "searchText is required",
                    "contracts": [],
                },
            )
        topstep = _topstep_adapter_for_admin()
        if not topstep._has_required_credentials():
            return JSONResponse(
                status_code=200,
                content={
                    "ok": False,
                    "status": "missing_credentials",
                    "message": "Topstep username/API key not configured",
                    "contracts": [],
                },
            )
        if not topstep._is_token_valid():
            auth_result = topstep.authenticate()
            if not auth_result.get("ok"):
                return JSONResponse(
                    status_code=200,
                    content={
                        "ok": False,
                        "status": auth_result.get("status", "auth_failed"),
                        "http_status": auth_result.get("http_status"),
                        "error_code": auth_result.get("error_code"),
                        "error_message": auth_result.get("error_message"),
                        "message": auth_result.get(
                            "message", "topstep auth failed"
                        ),
                        "contracts": [],
                    },
                )
        http_status, response = topstep._post_json(
            "/api/Contract/search",
            {"searchText": search_text, "live": live_flag},
            auth=True,
        )
        if http_status == 0:
            return JSONResponse(
                status_code=200,
                content={
                    "ok": False,
                    "status": "network_error",
                    "message": (
                        response
                        if isinstance(response, str)
                        else "topstep contract search network error"
                    ),
                    "contracts": [],
                },
            )
        if not isinstance(response, dict):
            return JSONResponse(
                status_code=200,
                content={
                    "ok": False,
                    "status": "contracts_failed",
                    "http_status": http_status,
                    "message": (
                        f"topstep contract search returned non-JSON ({http_status})"
                    ),
                    "contracts": [],
                },
            )
        if http_status >= 400 or response.get("success") is False:
            return JSONResponse(
                status_code=200,
                content={
                    "ok": False,
                    "status": "contracts_failed",
                    "http_status": http_status,
                    "error_code": response.get("errorCode"),
                    "error_message": response.get("errorMessage"),
                    "message": (
                        str(response.get("errorMessage"))
                        if response.get("errorMessage")
                        else f"topstep contract search failed ({http_status})"
                    ),
                    "contracts": [],
                },
            )
        raw_contracts = response.get("contracts")
        if not isinstance(raw_contracts, list):
            raw_contracts = []
        contracts: list[dict[str, Any]] = []
        for entry in raw_contracts:
            if not isinstance(entry, dict):
                continue
            contracts.append(
                {
                    "id": entry.get("id"),
                    "name": entry.get("name"),
                    "description": entry.get("description"),
                    "tickSize": entry.get("tickSize"),
                    "tickValue": entry.get("tickValue"),
                    "activeContract": entry.get("activeContract"),
                    "symbolId": entry.get("symbolId"),
                }
            )
        return JSONResponse(
            status_code=200,
            content={
                "ok": True,
                "status": "ok",
                "http_status": http_status,
                "message": f"{len(contracts)} contract(s)",
                "searchText": search_text,
                "live": live_flag,
                "contracts": contracts,
            },
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
        # Build the public Xiznit-style webhook URLs the operator needs
        # to paste into TradingView. The dashboard already requires an
        # admin session, so embedding the full secret here is OK — the
        # whole point of the page is to let the operator copy it.
        if secret_set:
            xiznit_url_local = (
                f"http://{settings.app_host}:{settings.app_port}"
                f"/webhooks/tradingview?secret={secret}&symbol={{{{ticker}}}}"
            )
            xiznit_url_tunnel = (
                "https://YOUR-TUNNEL-URL/webhooks/tradingview"
                f"?secret={secret}&symbol={{{{ticker}}}}"
            )
        else:
            xiznit_url_local = (
                f"http://{settings.app_host}:{settings.app_port}"
                "/webhooks/tradingview?secret=<set a secret above>"
                "&symbol={{ticker}}"
            )
            xiznit_url_tunnel = (
                "https://YOUR-TUNNEL-URL/webhooks/tradingview"
                "?secret=<set a secret above>&symbol={{ticker}}"
            )
        ctx.update(
            {
                "webhook_url": webhook_url,
                "host": settings.app_host,
                "port": settings.app_port,
                "secret_set": secret_set,
                "secret_value": secret if secret_set else "",
                "secret_preview": secret_preview,
                "alert_template": alert_template,
                "allowed_symbols": list(settings.allowed_symbols),
                "xiznit_url_local": xiznit_url_local,
                "xiznit_url_tunnel": xiznit_url_tunnel,
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
        # Never write the actual secret to the logs. We log only that a
        # regeneration happened so the audit trail is intact.
        log.info("tradingview webhook secret regenerated")
        return _flash_redirect(
            "/tradingview",
            "Webhook secret regenerated. Update both TradingView alert webhook URLs.",
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
        ctx["m"] = metrics_summary(journal=journal, broker=broker)
        ctx["order_history"] = {
            "lookback_days": settings.order_history_lookback_days,
            "limit": settings.order_history_limit,
        }
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
            # If the login succeeded against the legacy plaintext fallback
            # (no hash stored yet), migrate to a PBKDF2 hash now so the
            # plaintext default loses its grip on the next request. The
            # migration is best-effort — a write failure must not block
            # the user from logging in.
            stored_hash = settings.admin_password_hash or ""
            if not stored_hash:
                try:
                    hashed = hash_password(password)
                    settings_store.set_setting("ADMIN_PASSWORD_HASH", hashed)
                    settings.admin_password_hash = hashed
                    # Drop the in-memory plaintext so a future
                    # check_credentials never falls back to it.
                    settings.admin_password = ""
                    log.info(
                        "admin password migrated to hash on login"
                    )
                except Exception as exc:  # pragma: no cover - defensive
                    log.warning(
                        "admin password migration failed: %s",
                        exc.__class__.__name__,
                    )
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
