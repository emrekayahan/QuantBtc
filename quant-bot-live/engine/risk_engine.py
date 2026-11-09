"""Hard Deterministic Risk Gate, Quarter-Kelly Position Sizing, and Armed Emergency Kill-Switch."""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from config.settings import Settings, settings
from database.db_manager import DatabaseManager
from engine.exchange import BinanceExchangeWrapper
from engine.schemas import ReflexDecisionSchema

logger = logging.getLogger(__name__)


class RiskEngine:
    """Enforces non-negotiable execution gates, Quarter-Kelly position sizing, and emergency flatten."""

    def __init__(
        self,
        db: DatabaseManager,
        exchange: Optional[BinanceExchangeWrapper] = None,
        custom_settings: Optional[Settings] = None,
    ) -> None:
        self.db = db
        self.exchange = exchange
        self.cfg = custom_settings or settings

    def get_active_min_confidence(self) -> float:
        raw = self.db.get_state("min_confidence_threshold", str(self.cfg.MIN_CONFIDENCE_THRESHOLD))
        try:
            return float(raw) if raw is not None else float(self.cfg.MIN_CONFIDENCE_THRESHOLD)
        except ValueError:
            return float(self.cfg.MIN_CONFIDENCE_THRESHOLD)

    def get_active_max_position_usd(self) -> float:
        raw = self.db.get_state("max_position_usd", str(self.cfg.MAX_POSITION_USD))
        try:
            return float(raw) if raw is not None else float(self.cfg.MAX_POSITION_USD)
        except ValueError:
            return float(self.cfg.MAX_POSITION_USD)

    @staticmethod
    def calculate_quarter_kelly_fraction(
        direction_confidence: float,
        expected_reward_risk_ratio: float,
        round_trip_fee_pct: float = 0.20,
        stop_distance_pct: float = 0.015,
    ) -> float:
        """
        Calculate Quarter-Kelly safe fraction:
        1. Let p = direction_confidence, q = 1.0 - p, and b = expected_reward_risk_ratio (net of 0.20% round-trip fee buffer).
        2. Full Kelly fraction: f* = (b * p - q) / b
        3. Quarter-Kelly safe fraction: f_quarter = max(0.0, min(0.25, f* * 0.25))
        """
        p = max(0.0, min(1.0, float(direction_confidence)))
        q = 1.0 - p

        # Deduct round-trip fee buffer from reward-to-risk ratio
        fee_ratio_penalty = (round_trip_fee_pct / 100.0) / max(stop_distance_pct, 0.005)
        b = max(0.01, float(expected_reward_risk_ratio) - fee_ratio_penalty)

        f_star = (b * p - q) / b
        f_quarter = max(0.0, min(0.25, f_star * 0.25))
        return round(f_quarter, 6)

    def compute_position_size_usd(
        self,
        decision: ReflexDecisionSchema,
        free_usdt: float,
        min_notional_usd: float = 10.0,
    ) -> Dict[str, Any]:
        """Compute Quarter-Kelly position size in USD bounded by MAX_POSITION_USD and Binance minNotional * 1.10."""
        stop_dist_pct = abs(decision.entry_price - decision.invalidation_price) / max(
            decision.entry_price, 1e-9
        )
        f_quarter = self.calculate_quarter_kelly_fraction(
            direction_confidence=decision.direction_confidence,
            expected_reward_risk_ratio=decision.expected_reward_risk_ratio,
            round_trip_fee_pct=self.cfg.ROUND_TRIP_FEE_PCT,
            stop_distance_pct=stop_dist_pct,
        )
        max_pos_usd = self.get_active_max_position_usd()
        target_usd = min(float(free_usdt) * f_quarter, max_pos_usd)
        min_required_usd = float(min_notional_usd) * 1.10

        if target_usd < min_required_usd:
            return {
                "approved": False,
                "f_quarter": f_quarter,
                "position_usd": 0.0,
                "reason": (
                    f"Target USD ({target_usd:.2f}) below minNotional*1.10 ({min_required_usd:.2f})"
                ),
            }

        return {
            "approved": True,
            "f_quarter": f_quarter,
            "position_usd": round(target_usd, 2),
            "reason": "Quarter-Kelly sizing valid.",
        }

    def evaluate_gate(
        self,
        decision: ReflexDecisionSchema,
        free_usdt: float,
        min_notional_usd: float = 10.0,
    ) -> Dict[str, Any]:
        """
        Verify ALL non-negotiable execution gate conditions:
        - decision.direction == 'long'
        - decision.setup_quality >= 2
        - decision.direction_confidence >= 0.80 (or Brier-adjusted threshold)
        - decision.risk_state == 'safe' and decision.toxic_flow == 'none'
        - decision.regime != 'crisis'
        - open_positions < settings.MAX_OPEN_POSITIONS
        - system_halted == False
        """
        if self.db.is_system_halted():
            return {"approved": False, "position_usd": 0.0, "reason": "System is HALTED."}

        if self.db.is_symbol_paused(decision.symbol):
            return {
                "approved": False,
                "position_usd": 0.0,
                "reason": f"Symbol {decision.symbol} is temporarily paused.",
            }

        daily_pnl = self.db.get_daily_realized_pnl()
        if daily_pnl <= -abs(float(self.cfg.DAILY_MAX_LOSS_USD)):
            self.db.set_system_halted(
                True, reason=f"Daily max loss limit hit ({daily_pnl:.2f} USD)"
            )
            return {
                "approved": False,
                "position_usd": 0.0,
                "reason": f"Daily max loss reached ({daily_pnl:.2f} USD).",
            }

        drawdown_pct = float(self.db.get_state("current_drawdown_pct", "0.0") or 0.0)
        if drawdown_pct >= float(self.cfg.MAX_DRAWDOWN_PCT):
            self.db.set_system_halted(
                True, reason=f"Max drawdown limit hit ({drawdown_pct:.2f}%)"
            )
            return {
                "approved": False,
                "position_usd": 0.0,
                "reason": f"Max drawdown limit reached ({drawdown_pct:.2f}%).",
            }

        open_positions = self.db.get_open_positions()
        if len(open_positions) >= int(self.cfg.MAX_OPEN_POSITIONS):
            return {
                "approved": False,
                "position_usd": 0.0,
                "reason": f"Open positions ({len(open_positions)}) >= MAX_OPEN_POSITIONS ({self.cfg.MAX_OPEN_POSITIONS}).",
            }

        if any(p["symbol"] == decision.symbol for p in open_positions):
            return {
                "approved": False,
                "position_usd": 0.0,
                "reason": f"Position already open for {decision.symbol}.",
            }

        if decision.regime == "crisis":
            return {"approved": False, "position_usd": 0.0, "reason": "Macro regime is CRISIS."}

        if decision.direction != "long":
            return {
                "approved": False,
                "position_usd": 0.0,
                "reason": f"Direction is {decision.direction} (Spot Long only).",
            }

        if decision.setup_quality < int(self.cfg.MIN_SETUP_QUALITY):
            return {
                "approved": False,
                "position_usd": 0.0,
                "reason": f"Setup quality {decision.setup_quality} < {self.cfg.MIN_SETUP_QUALITY}.",
            }

        min_conf = self.get_active_min_confidence()
        if decision.direction_confidence < min_conf:
            return {
                "approved": False,
                "position_usd": 0.0,
                "reason": f"Confidence {decision.direction_confidence:.2f} < {min_conf:.2f}.",
            }

        if decision.risk_state != "safe":
            return {
                "approved": False,
                "position_usd": 0.0,
                "reason": f"Risk state is {decision.risk_state} (must be 'safe').",
            }

        if decision.toxic_flow != "none":
            return {
                "approved": False,
                "position_usd": 0.0,
                "reason": f"Toxic flow detected: {decision.toxic_flow}.",
            }

        sizing = self.compute_position_size_usd(
            decision=decision,
            free_usdt=free_usdt,
            min_notional_usd=min_notional_usd,
        )
        return sizing

    async def trigger_emergency_kill_switch(self, reason: str = "Operator Panic Stop") -> Dict[str, Any]:
        """
        Armed Emergency Kill-Switch:
        1. Set system_halted = True in DuckDB immediately.
        2. Cancel all open orders on Binance for open positions.
        3. Market-sell all open positions on Binance.
        """
        self.db.set_system_halted(True, reason=reason)
        open_positions = self.db.get_open_positions()
        cancelled_orders = 0
        flattened_positions = 0
        errors: list[str] = []

        for pos in open_positions:
            symbol = pos["symbol"]
            pos_id = pos["position_id"]
            qty = float(pos["quantity"])
            try:
                if self.exchange is not None:
                    cancelled = await self.exchange.cancel_all_orders(symbol)
                    cancelled_orders += len(cancelled or [])

                    # Check live free balance for symbol base asset if possible
                    base_asset = symbol.split("/")[0]
                    try:
                        bal = await self.exchange.fetch_balance()
                        free_token = float(bal.get("free", {}).get(base_asset, qty) or qty)
                        sell_qty = min(qty, free_token) if free_token > 0 else qty
                    except Exception:
                        sell_qty = qty

                    if sell_qty > 0:
                        sell_order = await self.exchange.create_market_sell_order(symbol, sell_qty)
                        exit_price = float(
                            sell_order.get("average")
                            or sell_order.get("price")
                            or pos["current_price"]
                        )
                    else:
                        exit_price = float(pos["current_price"])
                else:
                    exit_price = float(pos["current_price"])

                self.db.close_position(
                    position_id=pos_id,
                    exit_price=exit_price,
                    close_reason=f"KILL_SWITCH: {reason}",
                )
                flattened_positions += 1
            except Exception as exc:
                err_msg = f"Failed to flatten {symbol}: {exc}"
                logger.error(err_msg)
                errors.append(err_msg)

        return {
            "system_halted": True,
            "reason": reason,
            "cancelled_orders": cancelled_orders,
            "flattened_positions": flattened_positions,
            "errors": errors,
        }
