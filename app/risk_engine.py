"""Risk checks applied to every incoming signal before execution."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from .config import Settings
from .execution.broker_base import BrokerBase
from .journal import Journal
from .kill_switch import KillSwitch
from .schemas import NormalizedSignal

log = logging.getLogger("signalbridge.risk_engine")


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
    # Optional actionable hint for the operator. Composed into the
    # ``rejection_reason`` in the journal / webhook response when set
    # so the operator sees *how to fix it*, not just *what failed*.
    detail: Optional[str] = None


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
        *,
        broker: Optional[BrokerBase] = None,
    ) -> None:
        self.settings = settings
        self.journal = journal
        self.kill_switch = kill_switch
        # Optional — when provided and the active provider is topstep,
        # ``_open_position_symbols`` augments the journal-known set with
        # whatever the broker reports. See H3.
        self.broker = broker

    def evaluate(self, signal: NormalizedSignal) -> RiskDecision:
        s = self.settings
        execution_mode = (s.execution_mode or "off").lower()

        # Kill switch is only checked when the operator is Armed. In Off
        # state nothing submits anyway, and Test is for plumbing — the
        # operator should be able to verify payload construction even
        # while the kill switch is hot. The feature flag
        # ``ENABLE_KILL_SWITCH`` still governs whether the switch
        # functions at all.
        if execution_mode == "armed" and self.kill_switch.is_active():
            return RiskDecision(False, "kill_switch_active")

        # Single symbol allowlist applies in every state. The
        # pre-merge "stricter armed subset" idea was confusing the
        # operator — one box, one list, enforced uniformly.
        if signal.symbol not in s.allowed_symbols:
            return RiskDecision(False, f"symbol_not_allowed: {signal.symbol}")

        # Timeframe lock — optional. When off we don't even look at the
        # field, so older alerts that predate the lock keep working.
        if s.enable_timeframe_lock:
            incoming = normalize_timeframe(signal.timeframe)
            if incoming is None:
                return RiskDecision(
                    False,
                    "missing_timeframe",
                    detail=(
                        "timeframe required when timeframe lock is "
                        "enabled — include 'interval' (or 'timeframe' "
                        "/ 'tf') in the alert JSON, e.g. "
                        '"interval": "{{interval}}"'
                    ),
                )
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
        # H3: the count must include positions the bot didn't open
        # (e.g. operator manually opened a position in TopstepX), so we
        # merge the broker's open-position list with the journal's.
        if signal.action in LONG_ACTIONS | SHORT_ACTIONS:
            open_symbols = self._open_position_symbols()
            already_open_here = signal.symbol in open_symbols
            if not already_open_here:
                open_count = len(open_symbols)
                if open_count >= s.max_open_positions:
                    return RiskDecision(
                        False,
                        f"max_open_positions_reached ({open_count}/{s.max_open_positions})",
                    )

        return RiskDecision(True, None)

    def _open_position_symbols(self) -> set[str]:
        """Symbols currently considered open across the journal and the
        active broker. Topstep is queried via ``get_positions()`` so
        operator-initiated TopstepX positions count toward
        ``max_open_positions``.

        Tradeoff: when the broker call fails (network error / timeout)
        the gate falls open to journal-only — preferring to let a
        legitimate trade through during a transient outage rather than
        block the operator on a count we can't verify. The fallback
        path logs a WARNING so it's visible in the audit trail.

        Dedupe is by raw string key, so a TradingView ticker in the
        journal (``MES1!``) and a ProjectX contract id from the broker
        (``CON.F.US.MES.M26``) appear as two different entries — that
        OVER-counts when the same instrument is in both places, which
        fails closed (rejects when the operator may already be near
        the cap). Safe direction for a risk gate.
        """
        symbols: set[str] = set()
        for row in self.journal.list_open_positions():
            sym = row.get("symbol")
            if sym:
                symbols.add(str(sym))

        if self.broker is None or self.broker.provider != "topstep":
            return symbols

        try:
            resp = self.broker.get_positions()
        except Exception as exc:  # broad — adapter-defined errors vary
            log.warning(
                "broker.get_positions() failed during max_open_positions "
                "merge — falling back to journal-only count (%s: %s). "
                "Manual TopstepX positions may be under-counted.",
                exc.__class__.__name__,
                exc,
            )
            return symbols

        if not isinstance(resp, dict) or resp.get("ok") is not True:
            # missing_credentials / not_implemented envelopes aren't
            # errors — the operator simply hasn't connected the broker.
            # Don't log; the dashboard already surfaces that state.
            return symbols

        for pos in resp.get("positions") or []:
            if not isinstance(pos, dict):
                continue
            for key in ("symbol", "contractId", "contract_id"):
                value = pos.get(key)
                if value:
                    symbols.add(str(value))
                    break
        return symbols
