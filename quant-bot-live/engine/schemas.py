"""Pydantic Typed Schemas for Causal State Snapshots and Reflex Decisions."""

from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


RegimeLiteral = Literal["trending", "mean_reverting", "high_vol", "crisis"]
DirectionLiteral = Literal["long", "short", "neutral"]
ToxicFlowLiteral = Literal["none", "spoofing_suspected", "spread_widening", "volume_exhaustion"]
RiskStateLiteral = Literal["safe", "near_limit", "reduce"]


class StateSnapshotSchema(BaseModel):
    """Compact causal numeric market snapshot (<400 tokens) with zero future leakage."""

    model_config = ConfigDict(frozen=True)

    symbol: str
    causal_timestamp_ms: int = Field(gt=0, description="Timestamp of last CLOSED candle (iloc[-2])")
    regime: RegimeLiteral
    mid_price: float = Field(gt=0.0)
    best_bid: float = Field(gt=0.0)
    best_ask: float = Field(gt=0.0)
    spread_bps: float = Field(ge=0.0)
    orderbook_imbalance: float = Field(ge=-1.0, le=1.0)
    closed_open: float = Field(gt=0.0)
    closed_high: float = Field(gt=0.0)
    closed_low: float = Field(gt=0.0)
    closed_close: float = Field(gt=0.0)
    closed_volume: float = Field(ge=0.0)
    vol_sma_20: float = Field(ge=0.0)
    rsi_14: float = Field(ge=0.0, le=100.0)
    prev_rsi_14: float = Field(ge=0.0, le=100.0)
    ema_9: float = Field(gt=0.0)
    ema_21: float = Field(gt=0.0)
    ema_50: float = Field(gt=0.0)
    ema_200: float = Field(gt=0.0)
    bb_upper: float = Field(gt=0.0)
    bb_mid: float = Field(gt=0.0)
    bb_lower: float = Field(gt=0.0)
    adx_14: float = Field(ge=0.0)
    atr_14: float = Field(gt=0.0)
    realized_vol: float = Field(ge=0.0)
    toxic_flow: ToxicFlowLiteral
    open_positions_count: int = Field(ge=0)
    current_drawdown_pct: float = Field(ge=0.0)


class ReflexDecisionSchema(BaseModel):
    """Immutable typed proposal emitted by Layer 2 Reflex Scorer in <50ms."""

    model_config = ConfigDict(frozen=True)

    symbol: str
    causal_timestamp_ms: int
    regime: RegimeLiteral
    direction: DirectionLiteral
    toxic_flow: ToxicFlowLiteral
    setup_quality: int = Field(ge=0, le=3)
    direction_confidence: float = Field(ge=0.0, le=1.0)
    risk_state: RiskStateLiteral
    entry_price: float = Field(gt=0.0)
    invalidation_price: float = Field(gt=0.0, description="Hard Server-Side Stop-Loss Price")
    take_profit_price: float = Field(gt=0.0)
    expected_reward_risk_ratio: float = Field(gt=0.0)
    rationale: str
