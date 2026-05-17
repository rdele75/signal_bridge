"""Pydantic schemas for TradingView alerts and SignalBridge responses."""
from __future__ import annotations

from typing import Any, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator


# A field that may arrive as a quoted TradingView placeholder ("1", "5000.25")
# or as a raw JSON number from a hand-written client (1, 5000.25).
NumberLike = Union[str, int, float]


# ---------- Inbound (TradingView alert) ----------

class TradingViewAlert(BaseModel):
    """Raw alert payload as sent by TradingView.

    TradingView interpolates its placeholders as text, so numeric fields
    typically arrive as strings. Hand-rolled clients (curl tests, future
    integrations) often send raw JSON numbers instead — we accept both
    shapes and parse them downstream.
    """

    model_config = ConfigDict(extra="allow")

    secret: str
    source: Optional[str] = "tradingview"
    strategy: Optional[str] = None
    symbol: str
    exchange: Optional[str] = None
    action: str
    contracts: Optional[NumberLike] = None
    price: Optional[NumberLike] = None
    position_size: Optional[NumberLike] = None
    market_position: Optional[str] = None
    order_id: Optional[str] = None
    comment: Optional[str] = None
    bar_time: Optional[str] = None
    fire_time: Optional[str] = None

    @field_validator("contracts", "price", "position_size", mode="before")
    @classmethod
    def _validate_numberlike(cls, v: Any) -> Any:
        # Treat missing / blank as None so optional fields stay optional.
        if v is None or v == "":
            return None
        # bool is a subclass of int — reject it explicitly so True/False
        # can't sneak through as 1/0.
        if isinstance(v, bool):
            raise ValueError("expected number or numeric string, got bool")
        if isinstance(v, (int, float)):
            return v
        if isinstance(v, str):
            try:
                float(v)
            except ValueError as exc:
                raise ValueError(f"not a numeric value: {v!r}") from exc
            return v
        raise ValueError(
            f"expected number or numeric string, got {type(v).__name__}"
        )


# ---------- Internal normalized signal ----------

class NormalizedSignal(BaseModel):
    source: str = "tradingview"
    strategy: Optional[str] = None
    symbol: str  # TradingView ticker, e.g. "MES1!"
    broker_symbol: Optional[str] = None  # resolved per-provider, e.g. "MES" or "MESM26"
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
    broker_provider: str
    # Kept for backwards compatibility with anything that scrapes /status.
    broker: str
    allowed_symbols: list[str]
    kill_switch_active: bool
    open_positions: list[dict[str, Any]]
    database_path: str
