"""Parser/normalizer for inbound webhook payloads.

SignalBridge accepts two body shapes:

* ``generic_signalbridge`` — the historical schema with ``secret``,
  ``symbol``, ``action``, ``contracts``, ``price``. Used by hand-rolled
  alerts and any TradingView alert whose message we fully control.

* ``xiznit_native`` — the Xiznit Universal ORB strategy emits its own
  JSON via ``{{strategy.order.alert_message}}`` and
  ``{{strategy.alert_message}}``. The strategy owns the body, so the
  SignalBridge secret has to arrive via query string or header instead.
  This module pulls the relevant fields out and the webhook handler
  routes them into entry / TP exit / SL exit / stop-update branches.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


# Body keys that fully identify the legacy SignalBridge envelope. If
# every one of these is present the payload is treated as generic and
# flows through the original handler unchanged.
_GENERIC_HALLMARKS = ("secret", "symbol", "action", "contracts", "price")


# Xiznit native action verbs → coarse classification used by the
# webhook handler to pick a branch. ENTRY runs sizing/risk/broker;
# EXIT runs the close path; STOP_UPDATE is informational only.
_XIZNIT_ACTION_CLASS = {
    "buy": "ENTRY",
    "long": "ENTRY",
    "sell": "ENTRY",
    "short": "ENTRY",
    "exit": "EXIT",
    "close": "EXIT",
    "flatten": "EXIT",
    "update_sl": "STOP_UPDATE",
    "move_sl": "STOP_UPDATE",
    "sl_update": "STOP_UPDATE",
}


# Reasons coming back from the strategy that imply "flatten the whole
# position" when the body itself has no qty.
_CLOSE_ALL_REASONS = {
    "sl",
    "eod_flatten",
    "weekend_gap",
    "max_duration",
    "blackout",
}


def detect_payload_type(payload: Any) -> str:
    """Classify an inbound webhook body.

    Returns ``"generic_signalbridge"`` when the body looks like the
    legacy envelope, ``"xiznit_native"`` when it carries an ``action``
    field without our generic envelope, and ``"unknown"`` otherwise.
    """
    if not isinstance(payload, dict):
        return "unknown"
    if all(k in payload and payload[k] not in (None, "") for k in _GENERIC_HALLMARKS):
        return "generic_signalbridge"
    if "secret" in payload and payload.get("secret") not in (None, ""):
        # Body carries our secret → caller meant the generic envelope
        # even if it's missing some fields. The legacy handler will
        # surface the specific missing field.
        return "generic_signalbridge"
    if isinstance(payload.get("action"), str) and payload.get("action").strip():
        return "xiznit_native"
    return "unknown"


@dataclass
class XiznitParsed:
    """Normalized view of a Xiznit native payload.

    ``raw`` is the original body, preserved for journaling so nothing
    the strategy sent is dropped on the floor.
    """

    raw: dict[str, Any]
    action_class: str = ""  # ENTRY / EXIT / STOP_UPDATE / ""
    action_raw: str = ""
    symbol: Optional[str] = None
    qty: Optional[int] = None
    price: Optional[float] = None
    stop: Optional[float] = None
    tp_levels: dict[str, float] = field(default_factory=dict)
    tp_label: Optional[str] = None
    reason: Optional[str] = None
    order_id: Optional[str] = None
    comment: Optional[str] = None
    close_all: bool = False


def _first_non_empty(payload: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for k in keys:
        if k in payload and payload[k] not in (None, ""):
            return payload[k]
    return None


def _coerce_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _coerce_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def parse_xiznit_payload(
    payload: dict[str, Any],
    *,
    fallback_symbol: Optional[str] = None,
) -> XiznitParsed:
    """Extract SignalBridge-relevant fields from a Xiznit native body.

    ``fallback_symbol`` lets the caller pass the symbol via the
    request's query string so the strategy doesn't have to embed it.
    """
    parsed = XiznitParsed(raw=payload)

    action_raw = ""
    raw_action = payload.get("action")
    if isinstance(raw_action, str):
        action_raw = raw_action.strip().lower()
    parsed.action_raw = action_raw
    parsed.action_class = _XIZNIT_ACTION_CLASS.get(action_raw, "")

    sym = _first_non_empty(payload, ("symbol", "ticker"))
    parsed.symbol = _coerce_text(sym) or _coerce_text(fallback_symbol)

    parsed.qty = _coerce_int(_first_non_empty(payload, ("qty", "contracts", "size")))
    parsed.price = _coerce_float(
        _first_non_empty(payload, ("price", "entry", "fill_price"))
    )
    parsed.stop = _coerce_float(
        _first_non_empty(payload, ("sl", "stop", "stop_loss", "new_sl"))
    )

    for key in ("tp1", "tp2", "tp3"):
        if key in payload:
            val = _coerce_float(payload[key])
            if val is not None:
                parsed.tp_levels[key] = val

    # ``tp`` is overloaded: numeric for entry templates, label
    # ("TP1"/"TP2"/"TP3") for exit messages. Try both.
    tp_value = payload.get("tp")
    if tp_value is not None:
        as_float = _coerce_float(tp_value)
        if as_float is not None:
            parsed.tp_levels["tp"] = as_float
        if isinstance(tp_value, str):
            candidate = tp_value.strip().upper()
            if candidate in {"TP1", "TP2", "TP3"}:
                parsed.tp_label = candidate

    parsed.reason = _coerce_text(payload.get("reason"))

    oid = _first_non_empty(payload, ("order_id", "id"))
    parsed.order_id = _coerce_text(oid)
    parsed.comment = _coerce_text(payload.get("comment"))

    if (
        parsed.action_class == "EXIT"
        and parsed.qty is None
        and (parsed.reason or "").lower() in _CLOSE_ALL_REASONS
    ):
        parsed.close_all = True

    return parsed


def map_entry_action(action_raw: str) -> Optional[str]:
    """Map a Xiznit entry verb to the internal NormalizedSignal action."""
    a = (action_raw or "").strip().lower()
    if a in {"buy", "long"}:
        return "BUY"
    if a == "sell":
        # ``sell`` from the Xiznit strategy is an entry-short signal,
        # matching the legacy generic mapping.
        return "SELL"
    if a == "short":
        return "SHORT"
    return None
