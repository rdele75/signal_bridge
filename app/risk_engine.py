"""Risk checks applied to every incoming signal before execution."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .config import Settings
from .journal import Journal
from .kill_switch import KillSwitch
from .schemas import NormalizedSignal


# Map of incoming TradingView action -> internal normalized action.
ACTION_MAP = {
    "buy": "BUY",
    "long": "BUY",
    "sell": "SELL",
    "short": "SHORT",
    "exit": "EXIT",
    "close": "EXIT",
    "cover": "COVER",
}

LONG_ACTIONS = {"BUY"}
SHORT_ACTIONS = {"SELL", "SHORT"}
FLAT_ACTIONS = {"EXIT", "COVER"}


@dataclass
class RiskDecision:
    accepted: bool
    reason: Optional[str] = None


def normalize_action(raw_action: str) -> Optional[str]:
    if not raw_action:
        return None
    return ACTION_MAP.get(raw_action.strip().lower())


def parse_int(value, default: Optional[int] = None) -> Optional[int]:
    if value is None or value == "":
        return default
    try:
        # TradingView sends "1.0" sometimes — cast through float.
        return int(float(value))
    except (TypeError, ValueError):
        return default


def parse_float(value) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class RiskEngine:
    def __init__(
        self,
        settings: Settings,
        journal: Journal,
        kill_switch: KillSwitch,
    ) -> None:
        self.settings = settings
        self.journal = journal
        self.kill_switch = kill_switch

    def evaluate(self, signal: NormalizedSignal) -> RiskDecision:
        s = self.settings

        # Kill switch first — fail closed.
        if self.kill_switch.is_active():
            return RiskDecision(False, "kill_switch_active")

        # Symbol allow-list.
        if signal.symbol not in s.allowed_symbols:
            return RiskDecision(False, f"symbol_not_allowed: {signal.symbol}")

        # Contracts cap.
        if signal.contracts <= 0:
            return RiskDecision(False, "invalid_contracts")
        if signal.contracts > s.max_contracts_per_trade:
            return RiskDecision(
                False,
                f"contracts_above_max ({signal.contracts} > {s.max_contracts_per_trade})",
            )

        # Direction toggles.
        if signal.action in LONG_ACTIONS and not s.enable_longs:
            return RiskDecision(False, "longs_disabled")
        if signal.action in SHORT_ACTIONS and not s.enable_shorts:
            return RiskDecision(False, "shorts_disabled")

        # Daily loss limit.
        if s.max_daily_loss > 0:
            pnl = self.journal.get_daily_pnl()
            # Loss is a negative number; compare absolute.
            if pnl <= -abs(s.max_daily_loss):
                return RiskDecision(False, "daily_loss_limit_reached")

        # Duplicate order_id within cooldown.
        if signal.order_id:
            recent = self.journal.find_recent_order_id(
                signal.order_id,
                within_seconds=s.duplicate_order_cooldown_seconds,
            )
            if recent is not None:
                return RiskDecision(False, "duplicate_order_id")

        # Max open positions — only enforce for entries, not exits/covers.
        if signal.action in LONG_ACTIONS | SHORT_ACTIONS:
            existing = self.journal.get_position(signal.symbol)
            already_open_here = existing is not None and existing.get("quantity", 0) != 0
            if not already_open_here:
                open_count = self.journal.count_open_positions()
                if open_count >= s.max_open_positions:
                    return RiskDecision(
                        False,
                        f"max_open_positions_reached ({open_count}/{s.max_open_positions})",
                    )

        return RiskDecision(True, None)
