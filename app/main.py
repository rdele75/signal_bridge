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
    COLLAPSED_LEGACY_KEYS,
    RESTART_REQUIRED,
    SettingsStore,
    SettingsValidationError,
    detect_legacy_collapsed_keys,
    generate_secret,
    webhook_secret_preview,
)
from .execution.topstep import TopstepBroker
from .execution.topstep_order_builder import _generate_custom_tag
from .signal_router import (
    _topstep_token_sink,
    build_broker,
    refresh_topstep_credentials,
)
from .symbol_map import SymbolMap, parse_form_mappings
from .webhook import WebhookHandler


_HERE = Path(__file__).resolve().parent
TEMPLATES_DIR = _HERE / "templates"
STATIC_DIR = _HERE / "static"


EXECUTION_STATES: tuple[str, ...] = ("off", "test", "armed")


def _execution_card_view(
    *,
    settings: Settings,
    broker_snapshot: dict[str, Any],
    kill_switch,
) -> dict[str, Any]:
    """Build the Dashboard Execution card payload.

    One card, three states. The view surfaces blockers that would keep
    Arm from succeeding so the operator can read them off the page
    rather than discovering them after clicking.
    """
    state = (settings.execution_mode or "off").lower()
    if state not in EXECUTION_STATES:
        state = "off"
    selected_account_id = settings.resolved_account_id or ""
    selected_account_name = broker_snapshot.get("selected_account_name")
    selected_account_is_funded = broker_snapshot.get(
        "selected_account_is_funded"
    )
    can_trade = broker_snapshot.get("can_trade")

    armed_blockers: list[str] = []
    if not selected_account_id:
        armed_blockers.append("no selected Topstep account")
    if can_trade is False:
        armed_blockers.append("selected account canTrade=false")
    if settings.enable_kill_switch and kill_switch.is_active():
        armed_blockers.append("kill switch is active")
    if not settings.allowed_symbols:
        armed_blockers.append("allowed_symbols is empty")

    state_label = state.capitalize()
    if state == "armed":
        if selected_account_is_funded is True:
            funded_badge = "funded"
        elif selected_account_is_funded is False:
            funded_badge = "eval"
        else:
            funded_badge = "unknown"
    else:
        funded_badge = None

    return {
        "state": state,
        "state_label": state_label,
        "funded_badge": funded_badge,
        "selected_account_id": selected_account_id or None,
        "selected_account_name": selected_account_name,
        "selected_account_is_funded": selected_account_is_funded,
        "can_trade": can_trade,
        "kill_switch_active": kill_switch.is_active(),
        "kill_switch_enabled": settings.enable_kill_switch,
        "armed_blockers": armed_blockers,
        "can_arm": state != "armed" and not armed_blockers,
        "allowed_symbols": list(settings.allowed_symbols),
        "max_contracts_per_trade": settings.max_contracts_per_trade,
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
    # Boot-time schema check (execution-model-collapse, 2026-05-21).
    # Refuse to start against a pre-collapse SQLite — the legacy keys
    # would silently rebootstrap into the new MANAGED_KEYS shape and
    # leave the operator with a half-collapsed database. The wipe is
    # explicit: delete data/signalbridge.db and restart. The operator
    # was warned in the rework's pre-flight and exported the journal
    # to CSV.
    _legacy_keys = detect_legacy_collapsed_keys(settings_store)
    if _legacy_keys:
        log.critical(
            "SignalBridge refuses to start: the SQLite settings table "
            "contains pre-collapse keys %s — the database was "
            "bootstrapped against the older paper/demo/live model. "
            "Delete %s and restart so SignalBridge can bootstrap a "
            "fresh database from .env. The collapse is irreversible "
            "in this build; the operator's journal export from the "
            "pre-flight checklist is the source of truth for prior "
            "history.",
            _legacy_keys,
            settings.database_abs_path,
        )
        raise RuntimeError(
            "pre-collapse SQLite schema detected — see logs for "
            "instructions. Delete "
            f"{settings.database_abs_path} and restart."
        )
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
            "flash_kind": (
                flash_kind
                if flash_kind in {"ok", "error", "info", "warn"}
                else "info"
            ),
            "auth_enabled": settings.admin_auth_enabled,
        }

    def _flash_redirect(path: str, message: str, kind: str = "ok") -> RedirectResponse:
        from urllib.parse import urlencode
        qs = urlencode({"flash": message, "flash_kind": kind})
        return RedirectResponse(url=f"{path}?{qs}", status_code=303)

    def _settings_save_flash(
        path: str, success_message: str, changed_keys: list[str]
    ) -> RedirectResponse:
        """Flash redirect for settings POST handlers.

        Surfaces a "restart required" warn banner when any
        ``RESTART_REQUIRED`` key is in ``changed_keys``; otherwise emits
        the green success banner. Operator sees a single, honest line
        about what their save did or did not take effect.
        """
        restart_keys = [k for k in changed_keys if k in RESTART_REQUIRED]
        if restart_keys:
            joined = ", ".join(sorted(restart_keys))
            message = (
                f"{success_message} Restart required for: {joined}."
            )
            return _flash_redirect(path, message, kind="warn")
        return _flash_redirect(path, success_message, kind="ok")

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

        Re-uses the live broker when available (so its cached token
        sticks around). Mirrors current settings onto it so admin
        endpoints reflect runtime changes without a restart.
        """
        if isinstance(broker, TopstepBroker):
            broker.execution_mode = settings.execution_mode
            broker.allowed_symbols = list(settings.allowed_symbols)
            broker.max_contracts_per_trade = settings.max_contracts_per_trade
            broker.kill_switch_enabled = settings.enable_kill_switch
            broker.kill_switch_active = kill_switch.is_active()
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
            execution_mode=settings.execution_mode,
            allowed_symbols=settings.allowed_symbols,
            max_contracts_per_trade=settings.max_contracts_per_trade,
            kill_switch_enabled=settings.enable_kill_switch,
            kill_switch_active=kill_switch.is_active(),
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
    # Execution state endpoints + Topstep flatten/cancel
    #
    # ``/api/execution/{off,test,armed}`` flip the single
    # ``EXECUTION_MODE`` setting atomically. No confirmation tokens,
    # no acknowledgement checkboxes — the dropdown click is the
    # confirmation. The Armed transition runs a gate-stack check
    # before flipping; failing gates come back as a structured 400.
    #
    # ``/api/execution/submit-test-order`` always builds and validates
    # a 1-contract MES order against ProjectX, regardless of the
    # current execution state. Used as a smoke test from the
    # Dashboard.
    # ------------------------------------------------------------------

    def _set_execution_mode_and_apply(target: str) -> Any:
        try:
            coerced = settings_store.update_typed("EXECUTION_MODE", target)
        except SettingsValidationError as exc:
            return JSONResponse(
                status_code=400,
                content={
                    "ok": False,
                    "status": "invalid_execution_mode",
                    "message": str(exc),
                },
            )
        settings_store.apply_to_settings(
            settings, "EXECUTION_MODE", coerced
        )
        if isinstance(broker, TopstepBroker):
            broker.execution_mode = coerced
        log.info("execution mode set to %s", coerced)
        return coerced

    @app.post(
        "/api/execution/off", dependencies=[Depends(require_admin_api)]
    )
    def api_execution_off() -> JSONResponse:
        coerced = _set_execution_mode_and_apply("off")
        if isinstance(coerced, JSONResponse):
            return coerced
        return JSONResponse(
            status_code=200,
            content={
                "ok": True,
                "status": "execution_off",
                "execution_mode": coerced,
                "message": (
                    "Execution disengaged. Signals are journaled but no "
                    "orders submit."
                ),
            },
        )

    @app.post(
        "/api/execution/test", dependencies=[Depends(require_admin_api)]
    )
    def api_execution_test() -> JSONResponse:
        coerced = _set_execution_mode_and_apply("test")
        if isinstance(coerced, JSONResponse):
            return coerced
        return JSONResponse(
            status_code=200,
            content={
                "ok": True,
                "status": "execution_test",
                "execution_mode": coerced,
                "message": (
                    "Test mode active. Orders are built and validated "
                    "against ProjectX but not submitted."
                ),
            },
        )

    @app.post(
        "/api/execution/armed", dependencies=[Depends(require_admin_api)]
    )
    def api_execution_arm() -> JSONResponse:
        """Set execution mode to ``armed``.

        Runs the gate stack first — no selected account, kill switch
        on (when ENABLE_KILL_SWITCH=true), or an empty
        ALLOWED_SYMBOLS all refuse the flip with the failing
        gate label.
        """
        selected_account_id = settings.resolved_account_id or ""
        if not selected_account_id:
            return JSONResponse(
                status_code=400,
                content={
                    "ok": False,
                    "status": "no_selected_account",
                    "message": (
                        "no Topstep account selected — pick one in "
                        "/settings/broker before arming"
                    ),
                },
            )
        if settings.enable_kill_switch and kill_switch.is_active():
            return JSONResponse(
                status_code=400,
                content={
                    "ok": False,
                    "status": "kill_switch_active",
                    "message": (
                        "kill switch is active — deactivate it before "
                        "arming"
                    ),
                },
            )
        if not settings.allowed_symbols:
            return JSONResponse(
                status_code=400,
                content={
                    "ok": False,
                    "status": "no_allowed_symbols",
                    "message": (
                        "ALLOWED_SYMBOLS is empty — set the symbol "
                        "allowlist on /settings/risk before arming"
                    ),
                },
            )
        coerced = _set_execution_mode_and_apply("armed")
        if isinstance(coerced, JSONResponse):
            return coerced
        log.warning(
            "EXECUTION ARMED account=%s allowed_symbols=%s max_contracts=%s",
            settings.resolved_account_id,
            ",".join(settings.allowed_symbols),
            settings.max_contracts_per_trade,
        )
        try:
            journal.record_signal(
                source="admin",
                strategy="execution_armed",
                symbol=None,
                action="ARMED",
                contracts=None,
                price=None,
                order_id=None,
                raw_payload={"event": "execution_armed"},
                decision="accepted",
                rejection_reason=None,
                execution_mode="armed",
                execution_result={
                    "event": "execution_armed",
                    "account_id": settings.resolved_account_id or None,
                    "max_contracts_per_trade": (
                        settings.max_contracts_per_trade
                    ),
                    "allowed_symbols": list(settings.allowed_symbols),
                },
                broker_provider=broker.provider,
                broker_symbol=None,
                timeframe=None,
            )
        except Exception:  # pragma: no cover - audit best-effort
            log.warning(
                "execution arm: audit journal write failed", exc_info=True
            )
        return JSONResponse(
            status_code=200,
            content={
                "ok": True,
                "status": "execution_armed",
                "execution_mode": coerced,
                "selected_account_id": settings.resolved_account_id or None,
                "allowed_symbols": list(settings.allowed_symbols),
                "max_contracts_per_trade": settings.max_contracts_per_trade,
                "message": (
                    "Armed. Subsequent signals submit to the selected "
                    "Topstep account."
                ),
            },
        )

    @app.post(
        "/api/execution/submit-test-order",
        dependencies=[Depends(require_admin_api)],
    )
    async def api_execution_submit_test_order(
        request: Request,
    ) -> JSONResponse:
        """Smoke-test the broker plumbing with a synthetic 1-contract
        MES BUY (configurable via the JSON body).

        Always builds the payload — including against ProjectX (no
        POST). The current execution state does not matter: this
        endpoint is for verifying credentials, symbol mapping, and
        request shape before the operator arms.
        """
        from .schemas import NormalizedSignal

        try:
            body = await request.json()
        except Exception:
            body = None
        body = body if isinstance(body, dict) else {}

        symbol = str(body.get("symbol") or "MES1!").strip() or "MES1!"
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
                },
            )
        if contracts < 1:
            return JSONResponse(
                status_code=400,
                content={
                    "ok": False,
                    "status": "invalid_contracts",
                    "message": "contracts must be >= 1",
                },
            )

        broker_symbol = (
            symbol_map.resolve_explicit(symbol, "topstep")
            if symbol_map is not None
            else None
        )
        if not broker_symbol:
            return JSONResponse(
                status_code=400,
                content={
                    "ok": False,
                    "status": "symbol_mapping_missing",
                    "message": (
                        f"no Topstep contract id for {symbol!r} — add "
                        "one in /settings/symbols first"
                    ),
                },
            )
        signal = NormalizedSignal(
            source="admin",
            strategy="execution_submit_test_order",
            symbol=symbol,
            broker_symbol=broker_symbol,
            exchange=None,
            action=action_raw,
            contracts=contracts,
            price=None,
            order_id=None,
            comment="execution_submit_test_order",
            timeframe=None,
            raw=body,
        )
        topstep = _topstep_adapter_for_admin()
        preview = topstep.build_order_preview(signal, symbol_map=symbol_map)
        return JSONResponse(
            status_code=200,
            content={
                "ok": bool(preview.get("ok")),
                "submitted": False,
                "would_submit": False,
                "execution_mode": settings.execution_mode,
                "symbol": symbol,
                "broker_symbol": broker_symbol,
                "side": preview.get("side"),
                "size": preview.get("size"),
                "payload": preview.get("payload"),
                "reason": preview.get("reason"),
                "message": (
                    "Test order payload built — not submitted."
                    if preview.get("ok")
                    else f"Test order build failed: {preview.get('reason')}"
                ),
            },
        )

    @app.post(
        "/api/broker/flatten-all",
        dependencies=[Depends(require_admin_api)],
    )
    def api_broker_flatten_all() -> JSONResponse:
        """Flatten every open Topstep position via the broker adapter.

        Returns a structured per-leg envelope. Requires Armed mode —
        the adapter refuses the call in Off or Test states with a
        ``not_armed`` envelope (closing positions writes to a real
        account, which only happens in Armed).
        """
        if isinstance(broker, TopstepBroker):
            broker.execution_mode = settings.execution_mode
            broker.kill_switch_active = kill_switch.is_active()
            broker.kill_switch_enabled = settings.enable_kill_switch
        return JSONResponse(
            status_code=200, content=broker.flatten_position()
        )

    # M5 — per-process token bucket guarding the webhook. A misconfigured
    # TradingView alert template (or anyone with the secret) firing
    # tightly could otherwise saturate the broker / daily-loss limit.
    webhook_rate_limiter = TokenBucket(
        rate_per_second=settings.webhook_rate_limit_per_second,
        burst=settings.webhook_rate_burst,
    )
    app.state.webhook_rate_limiter = webhook_rate_limiter

    @app.post(
        "/api/tradingview/test-webhook",
        dependencies=[Depends(require_admin_api)],
    )
    def api_tradingview_test_webhook() -> JSONResponse:
        """Probe the local /webhooks/tradingview surface end-to-end.

        Builds a synthetic ``webhook_test=true`` payload with the
        currently-configured secret and dispatches it through the
        same WebhookHandler the real webhook uses. The handler's
        short-circuit verifies the secret and returns immediately
        without touching the risk engine, broker, or journal — so
        the operator can confirm reachability + signature handling
        without firing a real order.

        Returns a structured envelope the UI can render inline.
        """
        secret = (settings.webhook_secret or "").strip()
        if not secret or secret == "change_me_to_a_long_random_secret":
            return JSONResponse(
                status_code=200,
                content={
                    "ok": False,
                    "http_status": 0,
                    "response_body": "",
                    "message": (
                        "TRADINGVIEW_WEBHOOK_SECRET is not configured — "
                        "set a secret before testing the webhook."
                    ),
                },
            )
        payload = {
            "webhook_test": True,
            "secret": secret,
            "order_id": "webhook-test-",
        }
        response = handler.handle(payload)
        body_dict = response.model_dump(mode="json")
        if response.accepted and response.decision == "webhook_test":
            return JSONResponse(
                status_code=200,
                content={
                    "ok": True,
                    "http_status": 200,
                    "response_body": json.dumps(body_dict),
                    "message": (
                        "Webhook is reachable and accepting valid signatures."
                    ),
                },
            )
        return JSONResponse(
            status_code=200,
            content={
                "ok": False,
                "http_status": 200,
                "response_body": json.dumps(body_dict),
                "message": (
                    "Webhook test rejected: "
                    f"{response.rejection_reason or response.decision}"
                ),
            },
        )

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
        exec_view = _execution_card_view(
            settings=settings,
            broker_snapshot=broker_snapshot,
            kill_switch=kill_switch,
        )
        # Derive the execution-card state. Drives the CSS class on
        # the card and the headline status pill label.
        if settings.enable_kill_switch and kill_switch.is_active():
            exec_card_state = "kill-switch-active"
            exec_card_label = "Kill Switch Active"
        elif exec_view["state"] == "armed":
            exec_card_state = "armed"
            exec_card_label = "Armed"
        elif exec_view["state"] == "test":
            exec_card_state = "test"
            exec_card_label = "Test"
        else:
            exec_card_state = "off"
            exec_card_label = "Off"

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
                "execution": exec_view,
                "exec_card_state": exec_card_state,
                "exec_card_label": exec_card_label,
                "execution_mode_options": list(EXECUTION_STATES),
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
        broker_snapshot = broker_status_payload(settings=settings, broker=broker)
        # Topstep accounts are fetched via the explicit "Fetch accounts"
        # button on the page — pulling them on every render would
        # blow out to the network. Render an empty placeholder.
        accounts = {
            "ok": False,
            "provider": broker.provider,
            "not_implemented": False,
            "status": "not_loaded_on_render",
            "accounts": [],
            "message": "click 'Fetch accounts' to populate",
        }
        api_key = settings.topstep_api_key or ""
        api_key_preview = (
            f"…{api_key[-4:]}" if len(api_key) >= 4 else ""
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
                "realtime": realtime_view,
                "configured_execution_mode": settings.execution_mode,
                "selected_account_id": settings.resolved_account_id,
                "selected_account_id_raw": settings.selected_account_id,
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
        selected_account_id: str = Form(""),
        topstep_username: str = Form(""),
        topstep_api_key: str = Form(_TOPSTEP_API_KEY_UNCHANGED),
        topstep_env: str = Form("demo"),
    ):
        # BROKER_PROVIDER is pinned to topstep post-collapse, and the
        # execution state is owned by the Dashboard's mode dropdown —
        # neither is editable from this form. SELECTED_ACCOUNT_ID is
        # mirrored into TOPSTEP_ACCOUNT_ID so anything reading the env
        # var on the next boot picks up the same account.
        updates: list[tuple[str, Any]] = [
            ("SELECTED_ACCOUNT_ID", selected_account_id),
            ("TOPSTEP_ACCOUNT_ID", selected_account_id),
            ("TOPSTEP_USERNAME", topstep_username),
            ("TOPSTEP_ENV", topstep_env),
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
        # Mirror the saved credentials onto the live broker so the next
        # /api/broker/test-connection sees the new values without a
        # restart. ``refresh_topstep_credentials`` also clears the
        # cached auth token and canTrade cache — new creds imply a new
        # account context.
        refresh_topstep_credentials(broker, settings)

        return _settings_save_flash(
            "/settings/broker",
            "Broker settings saved.",
            list(coerced.keys()),
        )

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
            "allowed_symbols_csv": ",".join(settings.allowed_symbols),
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
        allowed_symbols: str = Form(""),
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
            ("ALLOWED_SYMBOLS", allowed_symbols),
        ]
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
        # Mirror the new symbol list onto the live broker.
        if isinstance(broker, TopstepBroker):
            broker.allowed_symbols = list(coerced["ALLOWED_SYMBOLS"])
            broker.max_contracts_per_trade = coerced["MAX_CONTRACTS_PER_TRADE"]
        # Defensive: if any future risk-form field starts writing a
        # TOPSTEP_* credential key, refresh the broker so the change
        # takes effect without restart. Today the risk form touches
        # only structural knobs, so this is a no-op guard.
        if any(key.startswith("TOPSTEP_") for key in coerced):
            refresh_topstep_credentials(broker, settings)
        return _settings_save_flash(
            "/settings/risk",
            "Risk settings saved.",
            list(coerced.keys()),
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
