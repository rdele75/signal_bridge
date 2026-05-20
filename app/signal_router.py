"""Pick the broker adapter to use based on configuration.

The provider is resolved from `BROKER_PROVIDER` first, then the legacy
`BROKER` env var, defaulting to "paper". Unknown provider names fall
back to paper — the safe default for a single-user bot.
"""
from __future__ import annotations

from typing import Optional

from .config import Settings
from .execution.broker_base import BrokerBase
from .execution.paper import PaperBroker
from .execution.topstep import TopstepBroker
from .journal import Journal
from .settings_store import SettingsStore


def _topstep_token_sink(
    settings: Settings, settings_store: SettingsStore
):
    """Persist freshly minted Topstep tokens back into SQLite + Settings.

    Called by ``TopstepBroker`` immediately after a successful loginKey
    response so the token survives a restart.
    """

    def _persist(token: str, expires_at: str) -> None:
        settings_store.set_setting("TOPSTEP_TOKEN", token)
        settings_store.set_setting("TOPSTEP_TOKEN_EXPIRES_AT", expires_at)
        settings.topstep_token = token
        settings.topstep_token_expires_at = expires_at

    return _persist


def build_broker(
    settings: Settings,
    journal: Journal,
    *,
    settings_store: Optional[SettingsStore] = None,
) -> BrokerBase:
    provider = settings.resolved_provider

    if provider == "paper":
        return PaperBroker(
            journal=journal,
            account_id=settings.resolved_account_id or "PAPER-001",
        )

    if provider == "topstep":
        token_sink = (
            _topstep_token_sink(settings, settings_store)
            if settings_store is not None
            else None
        )
        return TopstepBroker(
            username=settings.topstep_username,
            password=settings.topstep_password,
            api_key=settings.topstep_api_key,
            account_id=settings.resolved_account_id or settings.topstep_account_id,
            env=settings.topstep_env,
            base_url=settings.topstep_base_url,
            ws_url=settings.topstep_ws_url,
            token=settings.topstep_token,
            token_expires_at=settings.topstep_token_expires_at,
            token_sink=token_sink,
            enable_order_execution=settings.enable_topstep_order_execution,
            enable_order_dry_run=settings.enable_topstep_order_dry_run,
            execution_confirm=settings.topstep_execution_confirm,
            enable_live_trading=settings.enable_live_trading,
            execution_mode=settings.execution_mode,
            live_trading_confirm=settings.live_trading_confirm,
            live_trading_account_ack=settings.live_trading_account_ack,
            live_max_contracts_per_trade=settings.live_max_contracts_per_trade,
            live_allowed_symbols=settings.live_allowed_symbols,
            live_require_kill_switch_off=settings.live_require_kill_switch_off,
            max_contracts_per_trade=settings.max_contracts_per_trade,
        )

    # Unknown provider — fall back to paper rather than failing closed.
    return PaperBroker(
        journal=journal,
        account_id=settings.resolved_account_id or "PAPER-001",
    )
