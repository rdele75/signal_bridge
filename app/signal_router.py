"""Pick the broker adapter to use based on configuration.

The provider is resolved from `BROKER_PROVIDER` first, then the legacy
`BROKER` env var, defaulting to "paper". Unknown provider names fall
back to paper — the safe default for a single-user bot.
"""
from __future__ import annotations

from .config import Settings
from .execution.broker_base import BrokerBase
from .execution.paper import PaperBroker
from .execution.topstep import TopstepBroker
from .execution.tradovate import TradovateBroker
from .journal import Journal


def build_broker(settings: Settings, journal: Journal) -> BrokerBase:
    provider = settings.resolved_provider

    if provider == "paper":
        return PaperBroker(journal=journal)

    if provider == "topstep":
        return TopstepBroker(
            username=settings.topstep_username,
            password=settings.topstep_password,
            api_key=settings.topstep_api_key,
            account_id=settings.topstep_account_id,
            env=settings.topstep_env,
        )

    if provider == "tradovate":
        return TradovateBroker(
            username=settings.tradovate_username,
            password=settings.tradovate_password,
            app_id=settings.tradovate_app_id,
            app_version=settings.tradovate_app_version,
            cid=settings.tradovate_cid,
            sec=settings.tradovate_sec,
            account_id=settings.tradovate_account_id,
            env=settings.tradovate_env,
        )

    # Unknown provider — fall back to paper rather than failing closed.
    return PaperBroker(journal=journal)
