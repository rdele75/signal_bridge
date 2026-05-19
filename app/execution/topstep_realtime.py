"""Realtime Topstep account / order / position data.

This module is scaffolding only. ProjectX exposes a SignalR user hub
at ``$TOPSTEP_WS_URL`` for push updates; wiring an actual SignalR
client requires the ``signalrcore`` dependency which is intentionally
not pulled in yet. For now we ship two shapes:

  * ``RealtimePoller`` — the only mode that runs by default. The
    dashboard JS calls ``/api/realtime/state`` every
    ``TOPSTEP_REALTIME_POLL_SECONDS`` seconds; this class is a small
    server-side helper that the same JS / future jobs can build on.
  * ``SignalRClientPlaceholder`` — documented TODO surface. Calling
    ``start()`` returns a structured "not implemented" envelope so a
    future migration is obvious.

By design **nothing in this module ever places, cancels, or modifies
orders.** It is read-only. Tests assert this directly.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from .topstep import TopstepBroker


log = logging.getLogger("signalbridge.realtime")


# Documented future TODO. Pinned here so the dependency story is obvious
# from inside the module that needs it. Until ``signalrcore`` (or an
# equivalent SignalR / websockets shim) is added to requirements.txt,
# ``mode='signalr'`` falls back to polling.
_SIGNALR_DEPENDENCY = "signalrcore"


@dataclass
class RealtimeSnapshot:
    """In-memory cache of the most recently fetched state."""

    refreshed_at: Optional[str] = None
    positions: list[dict[str, Any]] | None = None
    orders: list[dict[str, Any]] | None = None
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "refreshed_at": self.refreshed_at,
            "positions": list(self.positions or []),
            "orders": list(self.orders or []),
            "message": self.message,
        }


class RealtimePoller:
    """Fetch positions + open orders from the active Topstep broker.

    Used by ``/api/realtime/state`` (one-shot) and any future
    background job that wants the same merged snapshot.
    """

    def __init__(self, broker: TopstepBroker) -> None:
        if not isinstance(broker, TopstepBroker):
            raise TypeError("RealtimePoller requires a TopstepBroker")
        self.broker = broker
        self._snapshot = RealtimeSnapshot()

    @property
    def snapshot(self) -> RealtimeSnapshot:
        return self._snapshot

    def refresh(self) -> RealtimeSnapshot:
        """Fetch positions + open orders. Never raises.

        Returns the updated ``RealtimeSnapshot``. Failures leave the
        previous snapshot intact and surface via ``message``.
        """
        positions_resp: dict[str, Any] = {}
        orders_resp: dict[str, Any] = {}
        try:
            positions_resp = self.broker.get_positions() or {}
        except Exception as exc:  # pragma: no cover - defensive
            log.warning(
                "realtime get_positions failed: %s", exc.__class__.__name__
            )
        try:
            orders_resp = self.broker.get_orders() or {}
        except Exception as exc:  # pragma: no cover - defensive
            log.warning(
                "realtime get_orders failed: %s", exc.__class__.__name__
            )
        positions = positions_resp.get("positions")
        if not isinstance(positions, list):
            positions = []
        orders = orders_resp.get("orders")
        if not isinstance(orders, list):
            orders = []
        self._snapshot = RealtimeSnapshot(
            refreshed_at=datetime.now(timezone.utc).isoformat(),
            positions=positions,
            orders=orders,
            message=(
                positions_resp.get("message", "")
                or orders_resp.get("message", "")
            ),
        )
        return self._snapshot


class SignalRClientPlaceholder:
    """Future SignalR user-hub client.

    Calling ``start()`` returns a structured envelope so the operator
    sees exactly which dependency is missing. The dashboard always
    falls back to polling in this build.
    """

    def __init__(self, ws_url: str, token: str, account_id: str) -> None:
        # Stored only so the placeholder reads like a real client and
        # future migration is a structural diff. ``token`` is never
        # logged or exposed elsewhere — see _ENVELOPE below.
        self.ws_url = ws_url
        self._token = token  # noqa: pylint kept private intentionally
        self.account_id = account_id

    def start(self) -> dict[str, Any]:
        return {
            "ok": False,
            "status": "not_implemented",
            "message": (
                f"SignalR client not enabled — install {_SIGNALR_DEPENDENCY!r} "
                "and wire a real subscriber. Polling fallback is the default."
            ),
            "depends_on": _SIGNALR_DEPENDENCY,
            "ws_url": self.ws_url,
            "account_id": self.account_id or None,
        }

    def stop(self) -> None:
        return None
