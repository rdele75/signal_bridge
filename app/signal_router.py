"""Construct the broker adapter used at runtime.

Post-collapse (2026-05-21) Topstep is the only supported provider.
``build_broker`` returns a fresh ``TopstepBroker`` configured from the
current ``Settings``. The pre-collapse paper-vs-topstep branching is
gone.
"""
from __future__ import annotations

from typing import Optional

from .config import Settings
from .execution.broker_base import BrokerBase
from .execution.topstep import DEFAULT_BASE_URL, DEFAULT_WS_URL, TopstepBroker
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
    """Build the Topstep adapter.

    ``journal`` is accepted for API compatibility with the pre-collapse
    paper-broker constructor but is unused — Topstep doesn't hydrate
    in-memory state from the journal. Removing the parameter would
    ripple through several callers; leaving it costs nothing.
    """
    del journal  # unused post-collapse
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
        execution_mode=settings.execution_mode,
        allowed_symbols=settings.allowed_symbols,
        max_contracts_per_trade=settings.max_contracts_per_trade,
        kill_switch_enabled=settings.enable_kill_switch,
    )


def refresh_topstep_credentials(
    broker: BrokerBase, settings: Settings
) -> bool:
    """Re-read Topstep credentials from ``settings`` onto the running
    broker so dashboard saves take effect without a restart.

    Returns True when the broker was a TopstepBroker (and so the
    refresh actually fired). The cached auth token and the canTrade
    cache are both invalidated: a new username/api-key implies a new
    auth context, and the old token is no longer trustworthy.
    """
    if not isinstance(broker, TopstepBroker):
        return False
    broker.username = (settings.topstep_username or "").strip()
    broker.api_key = (settings.topstep_api_key or "").strip()
    broker.account_id = (settings.topstep_account_id or "").strip()
    broker.env = (settings.topstep_env or "demo").strip().lower()
    broker.base_url = (
        (settings.topstep_base_url or DEFAULT_BASE_URL).strip().rstrip("/")
    )
    broker.ws_url = (
        (settings.topstep_ws_url or DEFAULT_WS_URL).strip().rstrip("/")
    )
    broker.token = ""
    broker.token_expires_at = ""
    broker._can_trade_cache.clear()
    broker._can_trade_warned = False
    return True
