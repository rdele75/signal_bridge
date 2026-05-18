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


def normalize_timeframe(value) -> Optional[str]:
    """Normalize a TradingView timeframe value for comparison.

    Numeric forms (1, "1", 1.0) collapse to "1". The common minute-suffix
    spelling ("5m", "15m") collapses to its numeric minute count.
    Hour-suffix ("1h") expands to minutes. Letter codes ("D", "W", "M")
    stay as uppercase single letters. Anything we can't classify is
    returned in uppercase so allow-list entries match in a stable form.
    """
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return None

    text = str(value).strip()
    if not text:
        return None

    # Bare integer ("1", "5", "60") → canonical numeric string.
    try:
        return str(int(text))
    except ValueError:
        pass
    try:
        f = float(text)
        if f.is_integer():
            return str(int(f))
    except ValueError:
        pass

    upper = text.upper()

    # Bare letter codes — case-insensitive: D / W / M (daily/weekly/monthly).
    if upper in {"D", "W", "M"}:
        return upper

    # Lowercase minute suffix ("1m", "5m", "15m") MUST be handled before
    # the uppercase letter-code branch — otherwise "1m" would collapse to
    # the monthly code "M".
    if text.endswith("m"):
        head = text[:-1].strip()
        try:
            return str(int(head))
        except ValueError:
            pass

    # Uppercase "1D"/"1W"/"1M" — keep the letter form for the 1× variant;
    # preserve longer numeric multipliers ("2D") as-is.
    if len(text) >= 2 and text[:-1].isdigit() and text[-1] in "DWM":
        head = text[:-1]
        suffix = text[-1]
        return suffix if head == "1" else f"{head}{suffix}"

    # Hour suffix ("1h" / "2H") — expand to minutes.
    if upper.endswith("H"):
        head = text[:-1].strip()
        try:
            return str(int(head) * 60)
        except ValueError:
            pass

    return upper


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

        # Timeframe lock — optional. When off we don't even look at the
        # field, so older alerts that predate the lock keep working.
        if s.enable_timeframe_lock:
            incoming = normalize_timeframe(signal.timeframe)
            if incoming is None:
                return RiskDecision(False, "missing_timeframe")
            allowed = [t for t in (normalize_timeframe(a) for a in s.allowed_timeframes) if t]
            if incoming not in allowed:
                allowed_str = ",".join(allowed) if allowed else "(empty)"
                return RiskDecision(
                    False,
                    f"timeframe_not_allowed: got {incoming} allowed {allowed_str}",
                )

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
