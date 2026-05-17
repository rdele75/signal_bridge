"""Pick the broker adapter to use based on configuration."""
from __future__ import annotations

from .config import Settings
from .execution.broker_base import BrokerBase
from .execution.paper import PaperBroker
from .execution.tradovate_demo import TradovateDemoBroker
from .execution.tradovate_live import TradovateLiveBroker
from .journal import Journal


def build_broker(settings: Settings, journal: Journal) -> BrokerBase:
    """Construct the broker adapter named in settings.

    Live trading is intentionally disabled — selecting it raises
    NotImplementedError from the adapter constructor.
    """
    broker = (settings.broker or "paper").lower()
    mode = (settings.execution_mode or "paper").lower()

    if broker == "paper" or mode == "paper":
        return PaperBroker(journal=journal)

    if broker == "tradovate_demo":
        return TradovateDemoBroker(
            username=settings.broker_username,
            password=settings.broker_password,
            account_id=settings.broker_account_id,
        )

    if broker == "tradovate_live":
        # Constructor raises NotImplementedError by design.
        return TradovateLiveBroker(
            username=settings.broker_username,
            password=settings.broker_password,
            account_id=settings.broker_account_id,
        )

    # Unknown broker — fall back to paper rather than failing closed,
    # since paper is the safe default for a single-user bot.
    return PaperBroker(journal=journal)
