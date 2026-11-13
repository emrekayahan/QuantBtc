"""K3 ExitEngine v2: ExitPlan, Chandelier/ATR ratcheting trailing, partial TP, and safe SL replacement."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("engine.exit_engine")


@dataclass
class ExitPlan:
    stop: float
    trail_type: str = "chandelier"  # "chandelier" | "atr" | "none"
    trail_mult: float = 2.5
    partial_tp: List[Tuple[float, float]] = field(default_factory=list)  # [(R_multiple, fraction_to_close)]
    breakeven_at_R: Optional[float] = None
    time_stop_bars: int = 24
    tp_maker: bool = True


class ExitManager:
    """Deterministic exit lifecycle manager: partial TPs, breakeven, monotonic trailing, safe SL replace."""

    @staticmethod
    def compute_trailed_stop(
        side: str,
        current_stop: float,
        extreme_price: float,
        atr: float,
        plan: ExitPlan,
    ) -> float:
        if plan.trail_type == "none" or atr <= 0.0 or plan.trail_mult <= 0.0:
            return float(current_stop)
        is_long = str(side).lower() == "long"
        if is_long:
            candidate = float(extreme_price) - float(plan.trail_mult) * float(atr)
            return max(float(current_stop), candidate)
        candidate = float(extreme_price) + float(plan.trail_mult) * float(atr)
        return min(float(current_stop), candidate)

    @staticmethod
    def init_state(
        entry_price: float,
        stop_price: float,
        side: str,
        quantity: float,
        plan: ExitPlan,
    ) -> Dict[str, Any]:
        risk_dist = max(abs(float(entry_price) - float(stop_price)), float(entry_price) * 1e-6)
        return {
            "entry_price": float(entry_price),
            "current_stop": float(stop_price),
            "side": str(side).lower(),
            "initial_qty": float(quantity),
            "remaining_qty": float(quantity),
            "risk_dist": risk_dist,
            "extreme_price": float(entry_price),
            "partial_done": [False] * len(plan.partial_tp),
            "breakeven_moved": False,
            "plan": plan,
        }

    def evaluate_bar(
        self,
        state: Dict[str, Any],
        bar_high: float,
        bar_low: float,
        bar_close: float,
        atr: float,
        bars_held: int,
    ) -> Dict[str, Any]:
        plan: ExitPlan = state["plan"]
        is_long = state["side"] == "long"
        entry = state["entry_price"]
        risk_dist = state["risk_dist"]

        if is_long:
            state["extreme_price"] = max(state["extreme_price"], float(bar_high))
            fav_r = (float(bar_high) - entry) / risk_dist
        else:
            state["extreme_price"] = min(state["extreme_price"], float(bar_low))
            fav_r = (entry - float(bar_low)) / risk_dist

        partial_close_qty = 0.0
        for idx, (target_r, frac) in enumerate(plan.partial_tp):
            if not state["partial_done"][idx] and fav_r >= float(target_r) and state["remaining_qty"] > 0.0:
                close_qty = min(state["remaining_qty"], state["initial_qty"] * float(frac))
                state["remaining_qty"] = max(0.0, state["remaining_qty"] - close_qty)
                state["partial_done"][idx] = True
                partial_close_qty += close_qty

        if plan.breakeven_at_R is not None and not state["breakeven_moved"] and fav_r >= float(plan.breakeven_at_R):
            if is_long:
                state["current_stop"] = max(state["current_stop"], entry)
            else:
                state["current_stop"] = min(state["current_stop"], entry)
            state["breakeven_moved"] = True

        new_sl = self.compute_trailed_stop(
            side=state["side"],
            current_stop=state["current_stop"],
            extreme_price=state["extreme_price"],
            atr=atr,
            plan=plan,
        )
        state["current_stop"] = new_sl

        time_exit = bars_held >= int(plan.time_stop_bars)
        return {
            "partial_close_qty": partial_close_qty,
            "new_stop": new_sl,
            "time_exit": time_exit,
            "close_price": float(bar_close),
        }

    async def replace_stop_safely(
        self,
        client: Any,
        symbol: str,
        side: str,
        quantity: float,
        new_stop_price: float,
        old_stop_order_id: Optional[str],
    ) -> Optional[str]:
        """
        Place the new STOP_MARKET (MARK_PRICE, reduceOnly) BEFORE cancelling the old SL.
        If placing the new SL fails, the old SL remains active so the position is never unprotected.
        """
        close_side = "sell" if str(side).lower() == "long" else "buy"
        try:
            new_order = await client.create_order(
                symbol=symbol,
                type="STOP_MARKET",
                side=close_side,
                amount=float(quantity),
                params={
                    "stopPrice": float(new_stop_price),
                    "workingType": "MARK_PRICE",
                    "reduceOnly": True,
                },
            )
            new_id = str(new_order.get("id") or "")
        except Exception as exc:
            logger.error("Failed to place new SL for %s at %.6f; keeping old SL %s: %s", symbol, new_stop_price, old_stop_order_id, exc)
            return old_stop_order_id

        if old_stop_order_id and new_id:
            try:
                await client.cancel_order(old_stop_order_id, symbol)
            except Exception as exc:
                logger.warning("Placed new SL %s for %s, failed to cancel old SL %s: %s", new_id, symbol, old_stop_order_id, exc)
        return new_id or old_stop_order_id
