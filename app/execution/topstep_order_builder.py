"""Topstep / ProjectX market-order payload builder.

Pure function: takes a ``NormalizedSignal`` plus enough context to look
up the broker contract id, and returns either a ready-to-POST
``/api/Order/place`` payload or a structured rejection envelope.

This module deliberately does NOT issue HTTP requests. Phase 2 uses it
to render dry-run previews; Phase 3 reuses the same payload when
demo-execution safety switches are on. Keeping construction
side-effect-free means the dry-run preview and the live submission can
never drift from one another, and the unit tests are trivial.

ProjectX order schema (subset we use):

    POST /api/Order/place
    {
      "accountId":   <int>,        # numeric ProjectX account id
      "contractId":  <str>,        # broker contract id (NOT a TV ticker)
      "type":        <int>,        # 2 = Market (see TYPE_MARKET)
      "side":        <int>,        # 0 = Bid/buy, 1 = Ask/sell
      "size":        <int>,        # contracts
      "limitPrice":  null,
      "stopPrice":   null,
      "trailPrice":  null,
      "customTag":   <str|null>    # truncated to <= CUSTOM_TAG_MAX_LEN
    }
"""
from __future__ import annotations

from typing import Any, Optional, Protocol

from ..schemas import NormalizedSignal


# ProjectX order types (only Market is wired up in this build).
TYPE_LIMIT = 1
TYPE_MARKET = 2
TYPE_STOP = 4
TYPE_TRAILING_STOP = 5
TYPE_JOIN_BID = 6
TYPE_JOIN_ASK = 7

# ProjectX sides.
SIDE_BUY = 0   # Bid / buy
SIDE_SELL = 1  # Ask / sell

# ProjectX customTag is metadata only; keep it short so a long
# strategy/comment combo never trips a wire-level limit.
CUSTOM_TAG_MAX_LEN = 64


# Internal -> ProjectX side. EXIT is intentionally not mapped here —
# it requires knowing the current position to pick a side, and the
# builder is meant to stay side-effect-free.
_ACTION_TO_SIDE: dict[str, int] = {
    "BUY": SIDE_BUY,
    "COVER": SIDE_BUY,
    "SELL": SIDE_SELL,
    "SHORT": SIDE_SELL,
}


class SymbolResolver(Protocol):
    """Minimal interface the builder needs from a symbol map.

    Implementations must return ``None`` when there is no explicit
    mapping for ``(ticker, provider)`` rather than echoing the input —
    a missing mapping is a hard rejection, not a fallback.
    """

    def resolve_explicit(
        self, ticker: Optional[str], provider: str
    ) -> Optional[str]: ...


def _rejection(reason: str, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ok": False,
        "would_submit": False,
        "reason": reason,
    }
    payload.update(extra)
    return payload


def _truncate_tag(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    text = str(value)
    if not text:
        return None
    if len(text) <= CUSTOM_TAG_MAX_LEN:
        return text
    return text[:CUSTOM_TAG_MAX_LEN]


def _parse_numeric_account_id(value: Any) -> Optional[int]:
    """Return ``int(value)`` when the input is a clean numeric id, else
    ``None``. Whitespace is stripped to mirror what ``TopstepBroker``
    already does on credentials."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return int(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def build_market_order_payload(
    signal: NormalizedSignal,
    *,
    account_id: Any,
    symbol_map: Optional[SymbolResolver] = None,
    provider: str = "topstep",
    custom_tag: Optional[str] = None,
) -> dict[str, Any]:
    """Build a ProjectX market-order payload from a normalized signal.

    On success, returns ``{"ok": True, "payload": {...}, "would_submit":
    False, ...}``. The ``would_submit`` flag is always False here —
    callers flip it themselves when they actually submit.

    On any rejection (unsupported action, missing mapping, non-numeric
    account id, zero contracts), returns ``{"ok": False, "reason": ...}``
    with enough context for the journal/UI to display without echoing
    secrets.
    """
    action = (signal.action or "").upper()

    if action == "EXIT":
        # EXIT can only be expressed as a real order side once we know
        # the current position direction. The builder stays read-only,
        # so reject and let the caller (or a future flatten-via-API
        # path) handle it.
        return _rejection(
            "unsupported_exit_without_position",
            symbol=signal.symbol,
            action=action,
        )

    side = _ACTION_TO_SIDE.get(action)
    if side is None:
        return _rejection(
            "unsupported_action",
            symbol=signal.symbol,
            action=action,
        )

    contracts = int(signal.contracts or 0)
    if contracts <= 0:
        return _rejection(
            "invalid_contracts",
            symbol=signal.symbol,
            action=action,
            contracts=contracts,
        )

    numeric_account_id = _parse_numeric_account_id(account_id)
    if numeric_account_id is None:
        return _rejection(
            "non_numeric_account_id",
            symbol=signal.symbol,
            action=action,
            account_id=str(account_id) if account_id is not None else "",
        )

    # Contract id: a ProjectX contract id is opaque (e.g.
    # ``CON.F.US.MES.M26``). We refuse to guess one — the operator must
    # configure an explicit mapping or pass a broker_symbol that is
    # already broker-shaped. The signal's ``broker_symbol`` field is
    # honored if it looks explicit (set, non-empty, different from the
    # raw TradingView ticker), otherwise we ask the symbol map for an
    # explicit Topstep entry.
    contract_id: Optional[str] = None
    if symbol_map is not None:
        contract_id = symbol_map.resolve_explicit(signal.symbol, provider)
    if not contract_id:
        # Honor an already-resolved broker_symbol that the webhook
        # handler attached via SymbolMap.resolve(). Skip when it equals
        # the raw ticker (i.e. SymbolMap had no entry and just echoed).
        bs = (signal.broker_symbol or "").strip()
        if bs and bs != signal.symbol:
            contract_id = bs
    if not contract_id:
        return _rejection(
            "symbol_mapping_missing",
            symbol=signal.symbol,
            action=action,
            provider=provider,
            message=(
                f"no explicit Topstep contract id configured for "
                f"{signal.symbol!r} — add an entry under "
                f"config/symbols.json: \"{signal.symbol}\": "
                f"{{\"topstep\": \"<ProjectX contract id>\"}}"
            ),
        )

    tag_source = custom_tag
    if tag_source is None:
        tag_source = signal.order_id or signal.comment
    custom_tag_value = _truncate_tag(tag_source)

    payload: dict[str, Any] = {
        "accountId": numeric_account_id,
        "contractId": contract_id,
        "type": TYPE_MARKET,
        "side": side,
        "size": contracts,
        "limitPrice": None,
        "stopPrice": None,
        "trailPrice": None,
        "customTag": custom_tag_value,
    }
    return {
        "ok": True,
        "would_submit": False,
        "payload": payload,
        "account_id": numeric_account_id,
        "contract_id": contract_id,
        "side": side,
        "size": contracts,
        "type": TYPE_MARKET,
        "symbol": signal.symbol,
        "broker_symbol": contract_id,
        "action": action,
    }
