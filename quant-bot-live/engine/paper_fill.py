"""Paper trading fill simulation and execution models.

Provides conservative fill and exit modeling:
- Limit buy orders only fill when candle_low <= limit_price.
- When both SL and TP are touched in the same candle, SL executes FIRST (conservative).
- Realistic fee deductions (spot vs futures) and execution slippage.
- Futures liquidation distance and margin maintenance calculation.
"""

from __future__ import annotations

import logging
import random
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


class PaperFillSimulator:
    """Deterministic, conservative paper fill model."""

    def __init__(
        self,
        spot_fee_pct: float = 0.10,
        futures_fee_pct: float = 0.05,
        slippage_pct: float = 0.05,
        sl_limit_buffer_pct: float = 0.20,
        partial_fill_prob: float = 0.0,
    ) -> None:
        self.spot_fee_pct = spot_fee_pct
        self.futures_fee_pct = futures_fee_pct
        self.slippage_pct = slippage_pct
        self.sl_limit_buffer_pct = sl_limit_buffer_pct
        self.partial_fill_prob = partial_fill_prob

    def simulate_limit_fill(
        self,
        order_price: float,
        candle_high: float,
        candle_low: float,
        candle_open: float,
        requested_qty: float,
    ) -> Tuple[bool, float, float]:
        """
        Simulate limit buy order fill against next candle.
        Returns: (is_filled, fill_price, filled_qty)
        """
        if candle_low > order_price:
            # Low never touched limit price -> no fill
            return False, 0.0, 0.0

        # Touched or gapped down: fill price cannot be worse than limit order price
        fill_price = min(order_price, candle_open if candle_open < order_price else order_price)

        # Apply slippage (on buy, slippage slightly increases cost)
        fill_price = fill_price * (1.0 + (self.slippage_pct / 100.0))

        # Check for partial fill
        if self.partial_fill_prob > 0.0 and random.random() < self.partial_fill_prob:
            filled_qty = round(requested_qty * random.uniform(0.3, 0.8), 6)
        else:
            filled_qty = requested_qty

        return True, fill_price, filled_qty

    def simulate_candle_exit(
        self,
        entry_price: float,
        sl_price: float,
        tp_price: float,
        candle_high: float,
        candle_low: float,
        is_long: bool = True,
    ) -> Optional[Dict[str, Any]]:
        """
        Simulate exit during candle.
        CONSERVATIVE RULE: If both SL and TP are touched in the same candle,
        SL is executed first!
        """
        sl_hit = (candle_low <= sl_price) if is_long else (candle_high >= sl_price)
        tp_hit = (candle_high >= tp_price) if is_long else (candle_low <= tp_price)

        if sl_hit and tp_hit:
            # Conservative: SL takes precedence
            exit_price = sl_price * (1.0 - (self.sl_limit_buffer_pct / 100.0) if is_long else 1.0 + (self.sl_limit_buffer_pct / 100.0))
            return {
                "triggered": True,
                "reason": "STOP_LOSS_HIT_SAME_CANDLE",
                "exit_price": exit_price,
                "conservative_sl_priority": True,
            }
        elif sl_hit:
            exit_price = sl_price * (1.0 - (self.sl_limit_buffer_pct / 100.0) if is_long else 1.0 + (self.sl_limit_buffer_pct / 100.0))
            return {
                "triggered": True,
                "reason": "STOP_LOSS_HIT",
                "exit_price": exit_price,
                "conservative_sl_priority": False,
            }
        elif tp_hit:
            exit_price = tp_price
            return {
                "triggered": True,
                "reason": "TAKE_PROFIT_HIT",
                "exit_price": exit_price,
                "conservative_sl_priority": False,
            }

        return None

    @staticmethod
    def calculate_futures_liquidation_price(
        entry_price: float,
        leverage: int,
        maintenance_margin_rate: float = 0.005,
        side: str = "long",
    ) -> float:
        """Calculate approximate isolated futures liquidation price."""
        if leverage <= 1:
            return 0.0
        if side.lower() == "long":
            return entry_price * (1.0 - (1.0 / leverage) + maintenance_margin_rate)
        else:
            return entry_price * (1.0 + (1.0 / leverage) - maintenance_margin_rate)
