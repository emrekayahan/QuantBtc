"""Hard Deterministic Risk Gate, Quarter-Kelly Position Sizing, and Armed Emergency Kill-Switch."""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from config.settings import Settings, settings
from database.db_manager import DatabaseManager
from engine.exchange import BinanceExchangeWrapper
from engine.risk_sizer import RiskSizer
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
        trading_mode: str = "SPOT",
        futures_free_usdt: float = 0.0,
    ) -> Dict[str, Any]:
        """Compute exact risk-based position size, margin, and leverage using RiskSizer."""
        raw_eq = float(self.db.get_state("total_equity_usd", "0.0") or 0.0)
        equity = max(raw_eq, float(free_usdt), float(getattr(self.cfg, "PAPER_STARTING_USDT", 30.0)))
        open_positions = self.db.get_open_positions()

        stop_dist_pct = abs(decision.entry_price - decision.invalidation_price) / max(
            decision.entry_price, 1e-9
        )
        f_quarter = self.calculate_quarter_kelly_fraction(
            direction_confidence=decision.direction_confidence,
            expected_reward_risk_ratio=decision.expected_reward_risk_ratio,
            round_trip_fee_pct=self.cfg.ROUND_TRIP_FEE_PCT,
            stop_distance_pct=stop_dist_pct,
        )

        return RiskSizer.calculate(
            equity=equity,
            entry_price=float(decision.entry_price),
            invalidation_price=float(decision.invalidation_price),
            min_notional=float(min_notional_usd),
            trading_mode=trading_mode,
            free_usdt=float(free_usdt),
            futures_free_usdt=float(futures_free_usdt),
            open_positions=open_positions,
            cfg=self.cfg,
            f_quarter=f_quarter,
        )

    def evaluate_gate(
        self,
        decision: ReflexDecisionSchema,
        free_usdt: float,
        min_notional_usd: float = 10.0,
        trading_mode: str = "SPOT",
        futures_free_usdt: float = 0.0,
    ) -> Dict[str, Any]:
        """
        Verify ALL non-negotiable execution gate conditions:
        - decision.direction == 'long'
        - decision.setup_quality >= 2
        - decision.direction_confidence >= 0.68 (or Brier-adjusted threshold)
        - decision.risk_state == 'safe' and decision.toxic_flow == 'none'
        - decision.regime != 'crisis'
        - open_positions < settings.MAX_OPEN_POSITIONS
        - daily_trades_count < settings.DAILY_MAX_TRADES
        - Dynamic daily max loss (DAILY_MAX_LOSS_PCT)
        - Max drawdown (MAX_DRAWDOWN_PCT)
        - system_halted == False
        - RiskSizer constraints (gross exposure with incoming trade, total open risk, liq buffer)
        """
        if self.db.is_system_halted():
            return {"approved": False, "position_usd": 0.0, "reason": "System is HALTED."}

        if self.db.is_safe_mode():
            return {
                "approved": False,
                "position_usd": 0.0,
                "reason": f"System is in SAFE MODE ({self.db.get_state('safe_mode_reason', 'Active anomaly')}); new entries blocked.",
            }

        if self.db.get_state("entries_blocked", "false") == "true":
            return {
                "approved": False,
                "position_usd": 0.0,
                "reason": "Entries are temporarily blocked due to active reconciliation violation.",
            }

        if self.db.is_symbol_paused(decision.symbol):
            return {
                "approved": False,
                "position_usd": 0.0,
                "reason": f"Symbol {decision.symbol} is temporarily paused.",
            }

        # Daily Trade Cap (0 = unlimited / continuous trading)
        if hasattr(self.db, "get_daily_trades_count"):
            daily_trades = self.db.get_daily_trades_count()
            max_daily_trades = int(getattr(self.cfg, "DAILY_MAX_TRADES", 0))
            if max_daily_trades > 0 and daily_trades >= max_daily_trades:
                return {
                    "approved": False,
                    "position_usd": 0.0,
                    "reason": f"Daily trade cap reached ({daily_trades}/{max_daily_trades} trades today).",
                }

        # Dynamic Daily Max Loss
        raw_eq = float(self.db.get_state("total_equity_usd", "0.0") or 0.0)
        equity = max(raw_eq, float(free_usdt), float(getattr(self.cfg, "PAPER_STARTING_USDT", 30.0)))
        is_small = equity < float(getattr(self.cfg, "SMALL_ACCOUNT_EQUITY_USD", 100.0))
        daily_loss_pct = float(getattr(self.cfg, "DAILY_MAX_LOSS_PCT", 3.0))
        if is_small:
            daily_loss_pct = max(daily_loss_pct, 5.0)
        effective_daily_max_loss = min(float(self.cfg.DAILY_MAX_LOSS_USD), equity * (daily_loss_pct / 100.0))

        daily_pnl = self.db.get_daily_realized_pnl()
        if daily_pnl <= -abs(effective_daily_max_loss):
            self.db.set_system_halted(
                True, reason=f"Daily max loss limit hit ({daily_pnl:.2f} USD <= -{effective_daily_max_loss:.2f} USD)"
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

        # Open positions & Correlated longs check
        open_positions = self.db.get_open_positions()
        max_longs = min(int(self.cfg.MAX_OPEN_POSITIONS), int(getattr(self.cfg, "MAX_CORRELATED_LONGS", 2)))
        if len(open_positions) >= max_longs:
            return {
                "approved": False,
                "position_usd": 0.0,
                "reason": f"Correlated longs cap reached ({len(open_positions)}/{max_longs}).",
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

        # Auto-detect Dual Wallet mode: switch to FUTURES if enabled and balance sufficient
        if trading_mode.upper() == "SPOT" and getattr(self.cfg, "ENABLE_FUTURES", False):
            futures_min = float(getattr(self.cfg, "FUTURES_MIN_USDT", 5.0))
            if float(futures_free_usdt) >= futures_min:
                trading_mode = "FUTURES"

        sizing = self.compute_position_size_usd(
            decision=decision,
            free_usdt=free_usdt,
            min_notional_usd=min_notional_usd,
            trading_mode=trading_mode,
            futures_free_usdt=futures_free_usdt,
        )
        return sizing

    async def trigger_emergency_kill_switch(self, reason: str = "Operator Panic Stop") -> Dict[str, Any]:
        """
        Armed Emergency Kill-Switch:
        1. Set system_halted = True in DuckDB immediately.
        2. In paper mode: cleanly close all paper positions in DB without exchange calls.
        3. In live mode: cancel open orders & market-sell both Spot and Futures positions.
        """
        self.db.set_system_halted(True, reason=reason)
        open_positions = self.db.get_open_positions()
        cancelled_orders = 0
        flattened_positions = 0
        errors: list[str] = []

        is_paper = getattr(self.cfg, "is_paper", True)

        for pos in open_positions:
            symbol = pos["symbol"]
            pos_id = pos["position_id"]
            qty = float(pos["quantity"])
            m_type = str(pos.get("market_type", "spot")).lower()

            if is_paper or pos.get("is_paper"):
                # Clean paper close without calling exchange
                exit_price = float(pos.get("current_price") or pos["entry_price"])
                self.db.close_position(
                    position_id=pos_id,
                    exit_price=exit_price,
                    close_reason=f"KILL_SWITCH_PAPER: {reason}",
                )
                flattened_positions += 1
                continue

            try:
                if self.exchange is not None:
                    # Futures position handling
                    if m_type == "futures" and getattr(self.exchange, "futures_client", None) is not None:
                        fut_sym = self.exchange.resolve_futures_symbol(symbol)
                        sl_oid = pos.get("sl_exchange_order_id")
                        if sl_oid:
                            try:
                                await self.exchange.futures_client.cancel_order(str(sl_oid), fut_sym, params={"stop": True})
                                cancelled_orders += 1
                            except Exception:
                                pass
                        # Market sell with reduceOnly=True
                        sell_order = await self.exchange.create_futures_order(
                            symbol=symbol,
                            order_type="market",
                            side="sell",
                            amount=qty,
                            price=None,
                            params={"reduceOnly": True},
                        )
                        exit_price = float(sell_order.get("average") or sell_order.get("price") or pos["current_price"])
                    else:
                        # Spot position handling
                        cancelled = await self.exchange.cancel_all_orders(symbol)
                        cancelled_orders += len(cancelled or [])
                        base_asset = symbol.split("/")[0]
                        try:
                            bal = await self.exchange.fetch_balance()
                            free_token = float(bal.get("free", {}).get(base_asset, qty) or qty)
                            sell_qty = min(qty, free_token) if free_token > 0 else qty
                        except Exception:
                            sell_qty = qty

                        if sell_qty > 0:
                            sell_order = await self.exchange.create_market_sell_order(symbol, sell_qty)
                            exit_price = float(sell_order.get("average") or sell_order.get("price") or pos["current_price"])
                        else:
                            exit_price = float(pos["current_price"])

                    self.db.close_position(
                        position_id=pos_id,
                        exit_price=exit_price,
                        close_reason=f"KILL_SWITCH_LIVE: {reason}",
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
