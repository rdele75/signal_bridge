"""Webhook handler — orchestrates secret check, normalization, risk, execution."""
from __future__ import annotations

import hmac
import logging
from typing import Any

from .config import Settings
from .execution.broker_base import BrokerBase
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

        contracts = parse_int(alert.contracts, default=1) or 0
        price = parse_float(alert.price)

        broker_symbol = (
            self.symbol_map.resolve(alert.symbol, self.broker.provider)
            if self.symbol_map is not None
            else alert.symbol
        )

        signal = NormalizedSignal(
            source=alert.source or "tradingview",
            strategy=alert.strategy,
            symbol=alert.symbol,
            broker_symbol=broker_symbol,
            exchange=alert.exchange,
            action=normalized_action,
            contracts=contracts,
            price=price,
            order_id=alert.order_id,
            comment=alert.comment,
            timeframe=normalize_timeframe(alert.timeframe),
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
                execution_result=None,
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

        # 7. Execute. Placeholder adapters (topstep/tradovate today) raise
        # NotImplementedError so we never silently no-op real trading.
        try:
            result: ExecutionResult = self.broker.execute(signal)
        except NotImplementedError as exc:
            reason = f"broker_not_implemented: {exc}"
            self._record_rejection(
                raw=raw_payload,
                reason=reason,
                symbol=signal.symbol,
                broker_symbol=signal.broker_symbol,
            )
            return WebhookResponse(
                accepted=False, decision="rejected", rejection_reason=reason
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
                execution_result=result.model_dump(),
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
            execution_result=result.model_dump(),
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

    def _record_rejection(
        self,
        *,
        raw: dict,
        reason: str,
        symbol: str | None = None,
        broker_symbol: str | None = None,
    ) -> None:
        tf_raw = raw.get("timeframe") if isinstance(raw, dict) else None
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
            execution_result=None,
            broker_provider=self.broker.provider,
            broker_symbol=broker_symbol,
            timeframe=normalize_timeframe(tf_raw),
        )
        log.info("REJECTED reason=%s", reason)
