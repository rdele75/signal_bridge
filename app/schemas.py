"""Pydantic schemas for TradingView alerts and SignalBridge responses."""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


# ---------- Inbound (TradingView alert) ----------

class TradingViewAlert(BaseModel):
    """Raw alert payload as sent by TradingView.

    All numeric fields arrive as strings because TradingView placeholders
    are interpolated as text. We parse them downstream.
    """

    model_config = ConfigDict(extra="allow")

    secret: str
    source: Optional[str] = "tradingview"
    strategy: Optional[str] = None
    symbol: str
    exchange: Optional[str] = None
    action: str
    contracts: Optional[str] = None
    price: Optional[str] = None
    position_size: Optional[str] = None
    market_position: Optional[str] = None
    order_id: Optional[str] = None
    comment: Optional[str] = None
    bar_time: Optional[str] = None
    fire_time: Optional[str] = None


# ---------- Internal normalized signal ----------

class NormalizedSignal(BaseModel):
    source: str = "tradingview"
    strategy: Optional[str] = None
    symbol: str
    exchange: Optional[str] = None
    action: str  # BUY / SELL / SHORT / COVER / EXIT
    contracts: int = 1
    price: Optional[float] = None
    order_id: Optional[str] = None
    comment: Optional[str] = None
    raw: dict[str, Any] = Field(default_factory=dict)


# ---------- Execution result ----------

class ExecutionResult(BaseModel):
    accepted: bool
    broker: str
    execution_mode: str
    symbol: str
    action: str
    contracts: int
    fill_price: Optional[float] = None
    order_id: Optional[str] = None
    message: str = ""
    position_after: Optional[dict[str, Any]] = None


# ---------- Outbound webhook response ----------

class WebhookResponse(BaseModel):
    accepted: bool
    decision: str  # "accepted" | "rejected"
    rejection_reason: Optional[str] = None
    execution: Optional[ExecutionResult] = None


# ---------- Status endpoint ----------

class StatusResponse(BaseModel):
    app_name: str
    execution_mode: str
    broker: str
    allowed_symbols: list[str]
    kill_switch_active: bool
    open_positions: list[dict[str, Any]]
    database_path: str
