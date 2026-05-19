"""Webhook handler — orchestrates secret check, normalization, risk, execution."""
from __future__ import annotations

import hmac
import logging
from typing import Any, Optional

from .config import Settings
from .execution.broker_base import BrokerBase
from .execution.topstep import TopstepBroker
from .journal import Journal
from .risk_engine import (
    RiskEngine,
    normalize_action,
    normalize_timeframe,
    parse_float,
    parse_int,
)
from .schemas import (
    ExecutionResult,
    NormalizedSignal,
    TradingViewAlert,
    WebhookResponse,
)
from .symbol_map import SymbolMap
from .webhook_parser import (
    XiznitParsed,
    detect_payload_type,
    map_entry_action,
    parse_xiznit_payload,
)

log = logging.getLogger("signalbridge.webhook")


# Legacy generic-envelope required fields. The historical handler only
# enforced three; bodies that look generic but miss ``contracts`` or
# ``price`` still fall through here and surface a clearer error
# downstream (e.g. ``missing_or_invalid_price`` from the paper broker).
REQUIRED_FIELDS = ("secret", "symbol", "action")


class WebhookHandler:
    def __init__(
        self,
        settings: Settings,
        journal: Journal,
        risk: RiskEngine,
        broker: BrokerBase,
        symbol_map: SymbolMap | None = None,
    ) -> None:
        self.settings = settings
        self.journal = journal
        self.risk = risk
        self.broker = broker
        self.symbol_map = symbol_map

    # ------------------------------------------------------------------

    def handle(
        self,
        raw_payload: Any,
        *,
        request_secret: Optional[str] = None,
        query_symbol: Optional[str] = None,
    ) -> WebhookResponse:
        """Process one webhook delivery.

        ``request_secret`` carries the secret pulled from the query
        string or ``X-SignalBridge-Secret`` header by the endpoint —
        used only when the body itself does not contain ``secret``.
        ``query_symbol`` is a similar fallback for Xiznit native alerts
        that pass the ticker through the URL.
        """
        # 1. Payload must be a JSON object.
        if not isinstance(raw_payload, dict):
            self._record_rejection(
                raw={"_invalid_payload": str(raw_payload)[:500]},
                reason="malformed_payload",
            )
            return WebhookResponse(
                accepted=False,
                decision="rejected",
                rejection_reason="malformed_payload",
            )

        payload_type = detect_payload_type(raw_payload)
        if payload_type == "generic_signalbridge":
            return self._handle_generic(raw_payload)
        if payload_type == "xiznit_native":
            return self._handle_xiznit(
                raw_payload,
                request_secret=request_secret,
                query_symbol=query_symbol,
            )

        self._record_rejection(raw=raw_payload, reason="malformed_payload")
        return WebhookResponse(
            accepted=False,
            decision="rejected",
            rejection_reason="malformed_payload",
        )

    # ------------------------------------------------------------------
    # Generic SignalBridge envelope (legacy path)
    # ------------------------------------------------------------------

    def _handle_generic(self, raw_payload: dict[str, Any]) -> WebhookResponse:
        # 2. Required fields must be present and non-empty.
        for field in REQUIRED_FIELDS:
            if field not in raw_payload or raw_payload.get(field) in (None, ""):
                reason = f"missing_required_field: {field}"
                self._record_rejection(raw=raw_payload, reason=reason)
                return WebhookResponse(
                    accepted=False, decision="rejected", rejection_reason=reason
                )

        # 3. Parse via Pydantic. Errors -> malformed.
        try:
            alert = TradingViewAlert.model_validate(raw_payload)
        except Exception as exc:  # pragma: no cover - defensive
            reason = f"malformed_payload: {exc.__class__.__name__}"
            self._record_rejection(raw=raw_payload, reason=reason)
            return WebhookResponse(
                accepted=False, decision="rejected", rejection_reason=reason
            )

        # 4. Constant-time secret check.
        if not hmac.compare_digest(alert.secret, self.settings.webhook_secret):
            self._record_rejection(raw=raw_payload, reason="invalid_secret")
            return WebhookResponse(
                accepted=False, decision="rejected", rejection_reason="invalid_secret"
            )

        # 5. Normalize action.
        normalized_action = normalize_action(alert.action)
        if normalized_action is None:
            reason = f"unknown_action: {alert.action}"
            self._record_rejection(raw=raw_payload, reason=reason, symbol=alert.symbol)
            return WebhookResponse(
                accepted=False, decision="rejected", rejection_reason=reason
            )

        alert_contracts_parsed = parse_int(alert.contracts, default=None)
        if alert_contracts_parsed is not None and alert_contracts_parsed <= 0:
            alert_contracts_parsed = None
        price = parse_float(alert.price)

        broker_symbol = (
            self.symbol_map.resolve(alert.symbol, self.broker.provider)
            if self.symbol_map is not None
            else alert.symbol
        )

        strategy_managed = bool(self.settings.strategy_managed_risk)
        fixed_contracts = int(self.settings.fixed_contracts_per_trade or 0)
        if strategy_managed:
            if alert_contracts_parsed is None:
                reason = "missing_or_invalid_alert_contracts"
                self._record_rejection(
                    raw=raw_payload,
                    reason=reason,
                    symbol=alert.symbol,
                    broker_symbol=broker_symbol,
                    alert_contracts=alert_contracts_parsed,
                    executed_contracts=None,
                    strategy_managed_risk=True,
                )
                return WebhookResponse(
                    accepted=False,
                    decision="rejected",
                    rejection_reason=reason,
                )
            executed_contracts = alert_contracts_parsed
        else:
            if fixed_contracts < 1:
                reason = "invalid_fixed_contracts_per_trade"
                self._record_rejection(
                    raw=raw_payload,
                    reason=reason,
                    symbol=alert.symbol,
                    broker_symbol=broker_symbol,
                    alert_contracts=alert_contracts_parsed,
                    executed_contracts=fixed_contracts,
                    strategy_managed_risk=False,
                )
                return WebhookResponse(
                    accepted=False,
                    decision="rejected",
                    rejection_reason=reason,
                )
            executed_contracts = fixed_contracts
            log.info(
                "alert contracts ignored; fixed sizing used: alert=%s fixed=%s",
                alert_contracts_parsed,
                executed_contracts,
            )

        signal = NormalizedSignal(
            source=alert.source or "tradingview",
            strategy=alert.strategy,
            symbol=alert.symbol,
            broker_symbol=broker_symbol,
            exchange=alert.exchange,
            action=normalized_action,
            contracts=executed_contracts,
            price=price,
            order_id=alert.order_id,
            comment=alert.comment,
            timeframe=normalize_timeframe(alert.timeframe),
            alert_contracts=alert_contracts_parsed,
            strategy_managed_risk=strategy_managed,
            raw=raw_payload,
        )

        return self._run_risk_and_execute(signal, raw_payload)

    # ------------------------------------------------------------------
    # Xiznit native payloads
    # ------------------------------------------------------------------

    def _handle_xiznit(
        self,
        raw_payload: dict[str, Any],
        *,
        request_secret: Optional[str],
        query_symbol: Optional[str],
    ) -> WebhookResponse:
        # The Xiznit strategy controls the JSON body, so the secret has
        # to come from the request envelope (query string or header).
        # Body ``secret`` (rare in this shape) still takes precedence so
        # tests/curl can drive the path without restating URL params.
        body_secret = raw_payload.get("secret")
        candidate_secret = (
            body_secret
            if isinstance(body_secret, str) and body_secret
            else request_secret
        )
        if not isinstance(candidate_secret, str) or not candidate_secret:
            self._record_rejection(raw=raw_payload, reason="missing_secret")
            return WebhookResponse(
                accepted=False,
                decision="rejected",
                rejection_reason="missing_secret",
            )
        if not hmac.compare_digest(candidate_secret, self.settings.webhook_secret):
            self._record_rejection(raw=raw_payload, reason="invalid_secret")
            return WebhookResponse(
                accepted=False,
                decision="rejected",
                rejection_reason="invalid_secret",
            )

        parsed = parse_xiznit_payload(raw_payload, fallback_symbol=query_symbol)

        if not parsed.action_class:
            reason = f"unknown_action: {parsed.action_raw or '(missing)'}"
            self._record_rejection(
                raw=raw_payload, reason=reason, symbol=parsed.symbol
            )
            return WebhookResponse(
                accepted=False, decision="rejected", rejection_reason=reason
            )

        if not parsed.symbol:
            self._record_rejection(
                raw=raw_payload, reason="missing_symbol"
            )
            return WebhookResponse(
                accepted=False,
                decision="rejected",
                rejection_reason="missing_symbol",
            )

        if parsed.action_class == "STOP_UPDATE":
            return self._handle_xiznit_stop_update(parsed, raw_payload)
        if parsed.action_class == "ENTRY":
            return self._handle_xiznit_entry(parsed, raw_payload)
        if parsed.action_class == "EXIT":
            return self._handle_xiznit_exit(parsed, raw_payload)

        # Defensive — _XIZNIT_ACTION_CLASS only emits the three above.
        reason = f"unknown_action_class: {parsed.action_class}"
        self._record_rejection(raw=raw_payload, reason=reason, symbol=parsed.symbol)
        return WebhookResponse(
            accepted=False, decision="rejected", rejection_reason=reason
        )

    # -- Xiznit branches -------------------------------------------------

    def _handle_xiznit_entry(
        self,
        parsed: XiznitParsed,
        raw_payload: dict[str, Any],
    ) -> WebhookResponse:
        normalized_action = map_entry_action(parsed.action_raw)
        if normalized_action is None:
            reason = f"unknown_action: {parsed.action_raw}"
            self._record_rejection(
                raw=raw_payload, reason=reason, symbol=parsed.symbol
            )
            return WebhookResponse(
                accepted=False, decision="rejected", rejection_reason=reason
            )

        if parsed.qty is None or parsed.qty <= 0:
            self._record_rejection(
                raw=raw_payload,
                reason="missing_or_invalid_qty",
                symbol=parsed.symbol,
            )
            return WebhookResponse(
                accepted=False,
                decision="rejected",
                rejection_reason="missing_or_invalid_qty",
            )

        broker_symbol = (
            self.symbol_map.resolve(parsed.symbol, self.broker.provider)
            if self.symbol_map is not None
            else parsed.symbol
        )

        strategy_managed = bool(self.settings.strategy_managed_risk)
        fixed_contracts = int(self.settings.fixed_contracts_per_trade or 0)
        if strategy_managed:
            executed_contracts = parsed.qty
        else:
            if fixed_contracts < 1:
                self._record_rejection(
                    raw=raw_payload,
                    reason="invalid_fixed_contracts_per_trade",
                    symbol=parsed.symbol,
                    broker_symbol=broker_symbol,
                    alert_contracts=parsed.qty,
                    executed_contracts=fixed_contracts,
                    strategy_managed_risk=False,
                )
                return WebhookResponse(
                    accepted=False,
                    decision="rejected",
                    rejection_reason="invalid_fixed_contracts_per_trade",
                )
            executed_contracts = fixed_contracts

        signal = NormalizedSignal(
            source="xiznit",
            strategy="xiznit_universal_orb",
            symbol=parsed.symbol,
            broker_symbol=broker_symbol,
            exchange=raw_payload.get("exchange"),
            action=normalized_action,
            contracts=executed_contracts,
            price=parsed.price,
            order_id=parsed.order_id,
            comment=parsed.comment,
            timeframe=normalize_timeframe(raw_payload.get("timeframe")),
            alert_contracts=parsed.qty,
            strategy_managed_risk=strategy_managed,
            raw=raw_payload,
        )

        if signal.price is None:
            # No price — paper broker can't synthesize a fill, and we
            # haven't built market-order routing for the other adapters
            # yet. Journal as an accepted dry-run so the operator sees
            # the alert landed and what we *would* have executed.
            return self._journal_dry_run(
                signal,
                raw_payload,
                message="xiznit_entry_dry_run_no_price",
                extra={"xiznit": self._xiznit_metadata(parsed)},
            )

        return self._run_risk_and_execute(
            signal,
            raw_payload,
            extra_execution_metadata={"xiznit": self._xiznit_metadata(parsed)},
        )

    def _handle_xiznit_exit(
        self,
        parsed: XiznitParsed,
        raw_payload: dict[str, Any],
    ) -> WebhookResponse:
        if not parsed.tp_label and not parsed.reason and not parsed.close_all:
            self._record_rejection(
                raw=raw_payload,
                reason="missing_exit_context",
                symbol=parsed.symbol,
            )
            return WebhookResponse(
                accepted=False,
                decision="rejected",
                rejection_reason="missing_exit_context",
            )

        if parsed.qty is None and not parsed.close_all:
            # TP exit (or non-close-all reason) with no qty — we won't
            # guess how much to flatten.
            self._record_rejection(
                raw=raw_payload,
                reason="missing_exit_qty",
                symbol=parsed.symbol,
            )
            return WebhookResponse(
                accepted=False,
                decision="rejected",
                rejection_reason="missing_exit_qty",
            )

        broker_symbol = (
            self.symbol_map.resolve(parsed.symbol, self.broker.provider)
            if self.symbol_map is not None
            else parsed.symbol
        )

        contracts_for_signal = parsed.qty if parsed.qty and parsed.qty > 0 else 1
        signal = NormalizedSignal(
            source="xiznit",
            strategy="xiznit_universal_orb",
            symbol=parsed.symbol,
            broker_symbol=broker_symbol,
            exchange=raw_payload.get("exchange"),
            action="EXIT",
            contracts=contracts_for_signal,
            price=parsed.price,
            order_id=parsed.order_id,
            comment=parsed.comment,
            timeframe=normalize_timeframe(raw_payload.get("timeframe")),
            alert_contracts=parsed.qty,
            strategy_managed_risk=bool(self.settings.strategy_managed_risk),
            raw=raw_payload,
        )

        xiznit_meta = self._xiznit_metadata(parsed)

        # When the strategy implies "flatten everything" (SL hit, EOD
        # flatten, weekend gap, etc.) we still journal it — but route
        # through the broker only when we have enough info to do so
        # safely (paper EXIT needs a price; close_all without qty is a
        # dry-run for now).
        if parsed.close_all or parsed.price is None:
            return self._journal_dry_run(
                signal,
                raw_payload,
                message=(
                    "xiznit_exit_dry_run_close_all"
                    if parsed.close_all
                    else "xiznit_exit_dry_run_no_price"
                ),
                extra={"xiznit": xiznit_meta},
            )

        return self._run_risk_and_execute(
            signal,
            raw_payload,
            extra_execution_metadata={"xiznit": xiznit_meta},
        )

    def _handle_xiznit_stop_update(
        self,
        parsed: XiznitParsed,
        raw_payload: dict[str, Any],
    ) -> WebhookResponse:
        """Record a stop-update notification without touching the broker.

        These messages exist so the operator can audit when the strategy
        moves its SL. They never submit an order; ``execution_result``
        carries the new stop level and ``decision`` is ``accepted``.
        """
        xiznit_meta = self._xiznit_metadata(parsed)
        execution_result = {
            "event": "stop_update_received",
            "stop_level": parsed.stop,
            "symbol": parsed.symbol,
            "broker_provider": self.broker.provider,
            "execution_mode": self.broker.execution_mode,
            "dry_run": True,
            "xiznit": xiznit_meta,
        }
        self.journal.record_signal(
            source="xiznit",
            strategy="xiznit_universal_orb",
            symbol=parsed.symbol,
            action="UPDATE_SL",
            contracts=None,
            price=None,
            order_id=parsed.order_id,
            raw_payload=raw_payload,
            decision="accepted",
            rejection_reason=None,
            execution_mode=self.broker.execution_mode,
            execution_result=execution_result,
            broker_provider=self.broker.provider,
            broker_symbol=(
                self.symbol_map.resolve(parsed.symbol, self.broker.provider)
                if self.symbol_map is not None
                else parsed.symbol
            ),
            timeframe=normalize_timeframe(raw_payload.get("timeframe")),
        )
        log.info(
            "XIZNIT stop_update_received symbol=%s stop=%s",
            parsed.symbol,
            parsed.stop,
        )
        return WebhookResponse(
            accepted=True,
            decision="accepted",
            rejection_reason=None,
            execution=ExecutionResult(
                accepted=True,
                broker=self.broker.provider,
                execution_mode=self.broker.execution_mode,
                symbol=parsed.symbol or "",
                action="UPDATE_SL",
                contracts=0,
                message="stop_update_received",
                details=execution_result,
            ),
        )

    # -- Shared risk+execute pipeline -----------------------------------

    def _run_risk_and_execute(
        self,
        signal: NormalizedSignal,
        raw_payload: dict[str, Any],
        *,
        extra_execution_metadata: Optional[dict[str, Any]] = None,
    ) -> WebhookResponse:
        decision = self.risk.evaluate(signal)
        if not decision.accepted:
            self.journal.record_signal(
                source=signal.source,
                strategy=signal.strategy,
                symbol=signal.symbol,
                action=signal.action,
                contracts=signal.contracts,
                price=signal.price,
                order_id=signal.order_id,
                raw_payload=raw_payload,
                decision="rejected",
                rejection_reason=decision.reason,
                execution_mode=self.broker.execution_mode,
                execution_result=self._risk_sizing_envelope(
                    signal, extra=extra_execution_metadata
                ),
                broker_provider=self.broker.provider,
                broker_symbol=signal.broker_symbol,
                timeframe=signal.timeframe,
            )
            log.info(
                "REJECTED symbol=%s action=%s reason=%s",
                signal.symbol,
                signal.action,
                decision.reason,
            )
            return WebhookResponse(
                accepted=False,
                decision="rejected",
                rejection_reason=decision.reason,
            )

        if isinstance(self.broker, TopstepBroker):
            result = self._execute_topstep(signal)
        else:
            try:
                result = self.broker.execute(signal)
            except NotImplementedError as exc:
                reason = f"broker_not_implemented: {exc}"
                self._record_rejection(
                    raw=raw_payload,
                    reason=reason,
                    symbol=signal.symbol,
                    broker_symbol=signal.broker_symbol,
                )
                return WebhookResponse(
                    accepted=False,
                    decision="rejected",
                    rejection_reason=reason,
                )

        if not result.accepted:
            reason = result.message or "broker_rejected"
            self.journal.record_signal(
                source=signal.source,
                strategy=signal.strategy,
                symbol=signal.symbol,
                action=signal.action,
                contracts=signal.contracts,
                price=signal.price,
                order_id=signal.order_id,
                raw_payload=raw_payload,
                decision="rejected",
                rejection_reason=reason,
                execution_mode=self.broker.execution_mode,
                execution_result=self._attach_risk_sizing(
                    result.model_dump(), signal, extra=extra_execution_metadata
                ),
                broker_provider=self.broker.provider,
                broker_symbol=signal.broker_symbol,
                timeframe=signal.timeframe,
            )
            log.info(
                "BROKER_REJECTED symbol=%s action=%s reason=%s",
                signal.symbol,
                signal.action,
                reason,
            )
            return WebhookResponse(
                accepted=False,
                decision="rejected",
                rejection_reason=reason,
                execution=result,
            )

        self.journal.record_signal(
            source=signal.source,
            strategy=signal.strategy,
            symbol=signal.symbol,
            action=signal.action,
            contracts=signal.contracts,
            price=signal.price,
            order_id=signal.order_id,
            raw_payload=raw_payload,
            decision="accepted",
            rejection_reason=None,
            execution_mode=self.broker.execution_mode,
            execution_result=self._attach_risk_sizing(
                result.model_dump(), signal, extra=extra_execution_metadata
            ),
            broker_provider=self.broker.provider,
            broker_symbol=signal.broker_symbol,
            timeframe=signal.timeframe,
        )
        log.info(
            "ACCEPTED symbol=%s action=%s contracts=%s price=%s",
            signal.symbol,
            signal.action,
            signal.contracts,
            signal.price,
        )
        return WebhookResponse(
            accepted=True,
            decision="accepted",
            rejection_reason=None,
            execution=result,
        )

    def _journal_dry_run(
        self,
        signal: NormalizedSignal,
        raw_payload: dict[str, Any],
        *,
        message: str,
        extra: Optional[dict[str, Any]] = None,
    ) -> WebhookResponse:
        """Record an accepted-but-not-executed Xiznit alert.

        Used when the strategy hands us an event we want to preserve in
        the journal but cannot route to the broker yet (missing price,
        close-all without qty, etc.).
        """
        execution_payload: dict[str, Any] = {
            "event": message,
            "dry_run": True,
            "broker_provider": self.broker.provider,
            "execution_mode": self.broker.execution_mode,
            "symbol": signal.symbol,
            "action": signal.action,
            "contracts": signal.contracts,
            "price": signal.price,
        }
        if extra:
            execution_payload.update(extra)
        self.journal.record_signal(
            source=signal.source,
            strategy=signal.strategy,
            symbol=signal.symbol,
            action=signal.action,
            contracts=signal.contracts,
            price=signal.price,
            order_id=signal.order_id,
            raw_payload=raw_payload,
            decision="accepted",
            rejection_reason=None,
            execution_mode=self.broker.execution_mode,
            execution_result=self._attach_risk_sizing(
                execution_payload, signal, extra=None
            ),
            broker_provider=self.broker.provider,
            broker_symbol=signal.broker_symbol,
            timeframe=signal.timeframe,
        )
        log.info(
            "XIZNIT %s symbol=%s action=%s contracts=%s",
            message,
            signal.symbol,
            signal.action,
            signal.contracts,
        )
        return WebhookResponse(
            accepted=True,
            decision="accepted",
            rejection_reason=None,
            execution=ExecutionResult(
                accepted=True,
                broker=self.broker.provider,
                execution_mode=self.broker.execution_mode,
                symbol=signal.symbol,
                action=signal.action,
                contracts=signal.contracts,
                fill_price=signal.price,
                order_id=signal.order_id,
                message=message,
                details=execution_payload,
            ),
        )

    @staticmethod
    def _xiznit_metadata(parsed: XiznitParsed) -> dict[str, Any]:
        return {
            "action_raw": parsed.action_raw,
            "action_class": parsed.action_class,
            "symbol": parsed.symbol,
            "qty": parsed.qty,
            "price": parsed.price,
            "stop": parsed.stop,
            "tp_levels": parsed.tp_levels,
            "tp_label": parsed.tp_label,
            "reason": parsed.reason,
            "order_id": parsed.order_id,
            "comment": parsed.comment,
            "close_all": parsed.close_all,
        }

    # ------------------------------------------------------------------

    def _execute_topstep(self, signal: NormalizedSignal) -> ExecutionResult:
        """Dispatch a normalized signal through the Topstep adapter.

        Outcomes:

        * ``ENABLE_TOPSTEP_ORDER_EXECUTION=false`` (default): build a
          dry-run preview, return ``accepted=True`` with
          ``message=topstep_dry_run_order_built`` and the built payload
          in ``details``. The order is NOT submitted.
        * ``EXECUTION_MODE=demo`` with demo gates lined up: POST
          ``/api/Order/place``, return the parsed ProjectX response.
        * ``EXECUTION_MODE=live`` with every live gate satisfied:
          POST ``/api/Order/place``. Any failing gate returns
          ``message=live_execution_locked`` with the specific
          ``gate`` label in ``details``.

        No paper fallback ever happens.
        """
        broker = self.broker
        assert isinstance(broker, TopstepBroker)
        provider = broker.provider
        execution_mode = self.settings.execution_mode

        # Mirror current settings onto the broker so the gate evaluation
        # sees the live runtime state (no restart needed for these flips).
        broker.enable_order_execution = (
            self.settings.enable_topstep_order_execution
        )
        broker.enable_order_dry_run = self.settings.enable_topstep_order_dry_run
        broker.execution_confirm = self.settings.topstep_execution_confirm
        broker.enable_live_trading = self.settings.enable_live_trading
        broker.execution_mode = execution_mode
        broker.live_trading_confirm = self.settings.live_trading_confirm
        broker.live_trading_account_ack = self.settings.live_trading_account_ack
        broker.live_max_contracts_per_trade = (
            self.settings.live_max_contracts_per_trade
        )
        broker.live_allowed_symbols = list(
            self.settings.live_allowed_symbols
        )
        broker.live_require_kill_switch_off = (
            self.settings.live_require_kill_switch_off
        )
        broker.max_contracts_per_trade = self.settings.max_contracts_per_trade
        broker.kill_switch_active = self.risk.kill_switch.is_active()

        if execution_mode == "live":
            gate = broker._live_execution_safety_check(signal)
            if gate is not None:
                details = {
                    "reason": "live_execution_locked",
                    "gate": gate,
                    "broker_provider": provider,
                    "execution_mode": execution_mode,
                    "enable_live_trading": self.settings.enable_live_trading,
                    "safety": broker._safety_state(),
                }
                log.info(
                    "LIVE_BLOCKED symbol=%s action=%s gate=%s",
                    signal.symbol,
                    signal.action,
                    gate,
                )
                return ExecutionResult(
                    accepted=False,
                    broker=provider,
                    execution_mode=execution_mode,
                    symbol=signal.symbol,
                    action=signal.action,
                    contracts=signal.contracts,
                    message="live_execution_locked",
                    details=details,
                )
            # All live gates passed — submit. ``submit_market_order``
            # re-runs the safety check; defense in depth.
            result = broker.submit_market_order(
                signal, symbol_map=self.symbol_map
            )
            accepted = bool(result.get("accepted"))
            order_id = result.get("broker_order_id") or result.get("order_id")
            message_label = (
                "topstep_live_order_submitted"
                if accepted
                else f"topstep_live_order_failed:{result.get('status')}"
            )
            log.info(
                "LIVE %s symbol=%s action=%s contracts=%s",
                "ACCEPTED" if accepted else "REJECTED",
                signal.symbol,
                signal.action,
                signal.contracts,
            )
            return ExecutionResult(
                accepted=accepted,
                broker=provider,
                execution_mode=execution_mode,
                symbol=signal.symbol,
                action=signal.action,
                contracts=signal.contracts,
                order_id=str(order_id) if order_id is not None else None,
                message=message_label,
                details=result,
            )

        if not self.settings.enable_topstep_order_execution:
            preview = broker.build_order_preview(
                signal, symbol_map=self.symbol_map
            )
            if not preview.get("ok"):
                details = dict(preview)
                details["would_submit"] = False
                details["dry_run"] = True
                return ExecutionResult(
                    accepted=False,
                    broker=provider,
                    execution_mode=execution_mode,
                    symbol=signal.symbol,
                    action=signal.action,
                    contracts=signal.contracts,
                    message=(
                        f"topstep_dry_run_build_failed:{preview.get('reason')}"
                    ),
                    details=details,
                )
            details = dict(preview)
            details["would_submit"] = False
            details["dry_run"] = True
            details["broker_provider"] = provider
            details["execution_mode"] = execution_mode
            return ExecutionResult(
                accepted=True,
                broker=provider,
                execution_mode=execution_mode,
                symbol=signal.symbol,
                action=signal.action,
                contracts=signal.contracts,
                message="topstep_dry_run_order_built",
                details=details,
            )

        result = broker.submit_market_order(
            signal, symbol_map=self.symbol_map
        )
        accepted = bool(result.get("accepted"))
        message_label = (
            "topstep_demo_order_submitted"
            if accepted
            else f"topstep_demo_order_failed:{result.get('status')}"
        )
        order_id = result.get("broker_order_id") or result.get("order_id")
        return ExecutionResult(
            accepted=accepted,
            broker=provider,
            execution_mode=execution_mode,
            symbol=signal.symbol,
            action=signal.action,
            contracts=signal.contracts,
            order_id=str(order_id) if order_id is not None else None,
            message=message_label,
            details=result,
        )

    # ------------------------------------------------------------------

    def _risk_sizing_envelope(
        self,
        signal: NormalizedSignal,
        *,
        extra: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        envelope: dict[str, Any] = {"risk_sizing": self._risk_sizing_dict(signal)}
        if extra:
            envelope.update(extra)
        return envelope

    def _attach_risk_sizing(
        self,
        execution_payload: dict[str, Any],
        signal: NormalizedSignal,
        *,
        extra: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        merged = dict(execution_payload) if execution_payload else {}
        merged["risk_sizing"] = self._risk_sizing_dict(signal)
        if extra:
            for key, value in extra.items():
                merged.setdefault(key, value)
        return merged

    @staticmethod
    def _risk_sizing_dict(signal: NormalizedSignal) -> dict[str, Any]:
        return {
            "alert_contracts": signal.alert_contracts,
            "executed_contracts": signal.contracts,
            "strategy_managed_risk": signal.strategy_managed_risk,
        }

    def _record_rejection(
        self,
        *,
        raw: dict,
        reason: str,
        symbol: str | None = None,
        broker_symbol: str | None = None,
        alert_contracts: int | None = None,
        executed_contracts: int | None = None,
        strategy_managed_risk: bool | None = None,
    ) -> None:
        tf_raw = raw.get("timeframe") if isinstance(raw, dict) else None
        sizing_known = (
            alert_contracts is not None
            or executed_contracts is not None
            or strategy_managed_risk is not None
        )
        execution_result: Optional[dict[str, Any]] = None
        if sizing_known:
            execution_result = {
                "risk_sizing": {
                    "alert_contracts": alert_contracts,
                    "executed_contracts": executed_contracts,
                    "strategy_managed_risk": strategy_managed_risk,
                }
            }
        # Strip a body-supplied secret out of the journaled payload —
        # we never want a real secret value lingering in raw_payload.
        if isinstance(raw, dict) and "secret" in raw:
            scrubbed = dict(raw)
            scrubbed["secret"] = "***"
            raw_for_journal: dict[str, Any] = scrubbed
        else:
            raw_for_journal = raw if isinstance(raw, dict) else {"_raw": str(raw)[:500]}

        self.journal.record_signal(
            source=raw.get("source") if isinstance(raw, dict) else None,
            strategy=raw.get("strategy") if isinstance(raw, dict) else None,
            symbol=symbol or (raw.get("symbol") if isinstance(raw, dict) else None),
            action=raw.get("action") if isinstance(raw, dict) else None,
            contracts=parse_int(raw.get("contracts")) if isinstance(raw, dict) else None,
            price=parse_float(raw.get("price")) if isinstance(raw, dict) else None,
            order_id=raw.get("order_id") if isinstance(raw, dict) else None,
            raw_payload=raw_for_journal,
            decision="rejected",
            rejection_reason=reason,
            execution_mode=self.broker.execution_mode,
            execution_result=execution_result,
            broker_provider=self.broker.provider,
            broker_symbol=broker_symbol,
            timeframe=normalize_timeframe(tf_raw),
        )
        log.info("REJECTED reason=%s", reason)
