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

log = logging.getLogger("signalbridge.webhook")


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

    def handle(self, raw_payload: Any) -> WebhookResponse:
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
        # Treat zero/negative as "missing or invalid" so strategy-managed
        # mode can refuse cleanly instead of letting it slip through as
        # the legacy default of 1.
        if alert_contracts_parsed is not None and alert_contracts_parsed <= 0:
            alert_contracts_parsed = None
        price = parse_float(alert.price)

        broker_symbol = (
            self.symbol_map.resolve(alert.symbol, self.broker.provider)
            if self.symbol_map is not None
            else alert.symbol
        )

        # 5b. Apply strategy-managed vs fixed sizing.
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

        # 6. Risk checks.
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
                execution_result=self._risk_sizing_envelope(signal),
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

        # 7. Execute. Provider-aware dispatch:
        #   * paper: broker.execute() as before
        #   * topstep: dry-run preview by default; conditional
        #     /api/Order/place when every safety gate is satisfied;
        #     hard reject on live mode.
        #   * any other adapter that still raises NotImplementedError
        #     surfaces as a clear ``broker_not_implemented`` rejection.
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
                    result.model_dump(), signal
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
                result.model_dump(), signal
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

    # ------------------------------------------------------------------

    def _execute_topstep(self, signal: NormalizedSignal) -> ExecutionResult:
        """Dispatch a normalized signal through the Topstep adapter.

        Three outcomes:

        * ``EXECUTION_MODE=live`` (or the hard kill is on): refuse with
          ``message=live_execution_locked``.
        * ``ENABLE_TOPSTEP_ORDER_EXECUTION=false`` (default): build a
          dry-run preview, return ``accepted=True`` with
          ``message=topstep_dry_run_order_built`` and the built payload
          in ``details``. The order is NOT submitted.
        * Demo execution gated true and every other safety switch lined
          up: POST ``/api/Order/place``, return the parsed ProjectX
          response in ``details``. ``accepted`` reflects whether the
          broker actually accepted the order.

        Either way, no paper fallback ever happens.
        """
        broker = self.broker
        assert isinstance(broker, TopstepBroker)
        provider = broker.provider
        execution_mode = self.settings.execution_mode

        if (
            execution_mode == "live"
            or self.settings.enable_live_trading
        ):
            return ExecutionResult(
                accepted=False,
                broker=provider,
                execution_mode=execution_mode,
                symbol=signal.symbol,
                action=signal.action,
                contracts=signal.contracts,
                message="live_execution_locked",
                details={
                    "reason": "live_execution_locked",
                    "broker_provider": provider,
                    "execution_mode": execution_mode,
                    "enable_live_trading": self.settings.enable_live_trading,
                },
            )

        if not self.settings.enable_topstep_order_execution:
            preview = broker.build_order_preview(
                signal, symbol_map=self.symbol_map
            )
            if not preview.get("ok"):
                # Builder rejection (missing symbol map, non-numeric
                # account id, unsupported action). No order is submitted
                # either way — surface the reason clearly.
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

        # Demo execution path. ``submit_market_order`` enforces the rest
        # of the safety chain (confirm token, demo mode, kill switch on
        # live trading, numeric account id) before touching the wire.
        # Mirror live settings onto the broker so runtime changes (the
        # operator flipping a switch after startup) take effect without
        # a restart.
        broker.enable_order_execution = (
            self.settings.enable_topstep_order_execution
        )
        broker.enable_order_dry_run = self.settings.enable_topstep_order_dry_run
        broker.execution_confirm = self.settings.topstep_execution_confirm
        broker.enable_live_trading = self.settings.enable_live_trading
        broker.execution_mode = execution_mode
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

    def _risk_sizing_envelope(self, signal: NormalizedSignal) -> dict[str, Any]:
        """Build a journal ``execution_result`` payload that only carries
        the sizing audit fields. Used for pre-broker rejections so the
        journal still records which mode was active and what the alert
        asked for."""
        return {"risk_sizing": self._risk_sizing_dict(signal)}

    def _attach_risk_sizing(
        self, execution_payload: dict[str, Any], signal: NormalizedSignal
    ) -> dict[str, Any]:
        """Return a copy of ``execution_payload`` with the sizing audit
        attached under ``risk_sizing``. ``signal.contracts`` is the
        post-sizing executed quantity; ``alert_contracts`` is what the
        alert asked for."""
        merged = dict(execution_payload) if execution_payload else {}
        merged["risk_sizing"] = self._risk_sizing_dict(signal)
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
        self.journal.record_signal(
            source=raw.get("source") if isinstance(raw, dict) else None,
            strategy=raw.get("strategy") if isinstance(raw, dict) else None,
            symbol=symbol or (raw.get("symbol") if isinstance(raw, dict) else None),
            action=raw.get("action") if isinstance(raw, dict) else None,
            contracts=parse_int(raw.get("contracts")) if isinstance(raw, dict) else None,
            price=parse_float(raw.get("price")) if isinstance(raw, dict) else None,
            order_id=raw.get("order_id") if isinstance(raw, dict) else None,
            raw_payload=raw if isinstance(raw, dict) else {"_raw": str(raw)[:500]},
            decision="rejected",
            rejection_reason=reason,
            execution_mode=self.broker.execution_mode,
            execution_result=execution_result,
            broker_provider=self.broker.provider,
            broker_symbol=broker_symbol,
            timeframe=normalize_timeframe(tf_raw),
        )
        log.info("REJECTED reason=%s", reason)
