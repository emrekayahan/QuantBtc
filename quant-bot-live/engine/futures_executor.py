"""K5: FuturesExecutor v2 — one-way/leverage readback, isolated margin, SL <=5s, liq verification."""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from config.settings import Settings, settings

logger = logging.getLogger("engine.futures_executor")


class FuturesExecutor:
    """Safe USD-M Futures entry executor enforcing readback, SL protection, and liq buffer."""

    def __init__(self, client: Any, custom_settings: Optional[Settings] = None) -> None:
        self.client = client
        self.cfg = custom_settings or settings

    @staticmethod
    def _fut_symbol(symbol: str) -> str:
        return symbol if ":" in symbol else f"{symbol}:USDT"

    async def execute_futures_entry(
        self,
        symbol: str,
        side: str,
        quantity: float,
        entry_price: float,
        stop_price: float,
        target_leverage: int,
        entry_type: str = "MARKET",
    ) -> Dict[str, Any]:
        fut_sym = self._fut_symbol(symbol)
        is_long = str(side).lower() == "long"
        entry_side = "buy" if is_long else "sell"
        close_side = "sell" if is_long else "buy"

        # 1. Verify one-way mode (hedge mode OFF)
        try:
            pos_mode = await self.client.fetch_position_mode(fut_sym)
            if isinstance(pos_mode, dict) and bool(pos_mode.get("hedged", False)):
                return {"executed": False, "reject_reason": "hedge_mode_enabled"}
        except Exception as exc:
            logger.warning("fetch_position_mode failed for %s: %s", fut_sym, exc)

        # 2. Set isolated margin & target leverage, then read back
        try:
            await self.client.set_margin_mode("isolated", fut_sym)
        except Exception:
            pass  # Binance returns error if already isolated
        await self.client.set_leverage(int(target_leverage), fut_sym)

        positions = await self.client.fetch_positions([fut_sym])
        if positions:
            actual_lev = int(float(positions[0].get("leverage") or 0))
            if actual_lev != int(target_leverage):
                return {
                    "executed": False,
                    "reject_reason": "leverage_readback_mismatch",
                    "actual_leverage": actual_lev,
                    "target_leverage": int(target_leverage),
                }

        # 3. Place entry order
        entry_order = await self.client.create_order(
            symbol=fut_sym,
            type=str(entry_type).lower(),
            side=entry_side,
            amount=float(quantity),
            price=float(entry_price) if str(entry_type).upper() == "LIMIT" else None,
        )
        filled_qty = float(entry_order.get("filled") or quantity)
        avg_entry = float(entry_order.get("average") or entry_price)

        # 4. Place protective SL (STOP_MARKET, MARK_PRICE, reduceOnly) immediately
        sl_order = await self.client.create_order(
            symbol=fut_sym,
            type="STOP_MARKET",
            side=close_side,
            amount=filled_qty,
            params={
                "stopPrice": float(stop_price),
                "workingType": "MARK_PRICE",
                "reduceOnly": True,
            },
        )

        # 5. Verify liquidation price buffer after open (LIQ_BUFFER_X * stop_dist)
        post_pos = await self.client.fetch_positions([fut_sym])
        if post_pos:
            liq_px = float(post_pos[0].get("liquidationPrice") or 0.0)
            if liq_px > 0.0:
                stop_dist = abs(avg_entry - float(stop_price))
                req_dist = float(self.cfg.LIQ_BUFFER_X) * stop_dist
                liq_dist = (avg_entry - liq_px) if is_long else (liq_px - avg_entry)
                if liq_dist < req_dist:
                    logger.critical(
                        "Liquidation buffer breach on %s! liq_dist=%.4f < req=%.4f. Closing immediately.",
                        fut_sym, liq_dist, req_dist,
                    )
                    await self.client.create_order(
                        symbol=fut_sym,
                        type="market",
                        side=close_side,
                        amount=filled_qty,
                        params={"reduceOnly": True},
                    )
                    return {"executed": False, "reject_reason": "liquidation_buffer_breach"}

        return {
            "executed": True,
            "symbol": fut_sym,
            "side": side,
            "entry_order": entry_order,
            "sl_order": sl_order,
            "filled_qty": filled_qty,
            "entry_price": avg_entry,
            "leverage": int(target_leverage),
        }
