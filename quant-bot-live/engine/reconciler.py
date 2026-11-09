"""30-second State Reconciler between DuckDB and Binance Spot."""

from __future__ import annotations

import asyncio

import logging
from typing import Any, Dict, Optional

from config.settings import Settings, settings
from database.db_manager import DatabaseManager
from engine.exchange import BinanceExchangeWrapper
from engine.execution import ExecutionEngine

logger = logging.getLogger(__name__)


class StateReconciler:
    """Synchronizes DuckDB open positions, server-side stop-losses, and USDT equity every 30 seconds."""

    def __init__(
        self,
        exchange: BinanceExchangeWrapper,
        db: DatabaseManager,
        execution: Optional[ExecutionEngine] = None,
        custom_settings: Optional[Settings] = None,
        telegram: Optional[Any] = None,
    ) -> None:
        self.exchange = exchange
        self.db = db
        self.execution = execution
        self.cfg = custom_settings or settings
        self.telegram = telegram

    async def reconcile_cycle(self) -> Dict[str, Any]:
        """
        1. Fetch live Binance balance and tickers.
        2. Update current prices, unrealized PnL, total USDT equity, peak equity, and drawdown %.
        3. Check if any open position hit take_profit_price or if its Server-Side SL filled on Binance.
        """
        balance = await self.exchange.fetch_balance()
        free_usdt = float(balance.get("free", {}).get("USDT", 0.0) or 0.0)
        total_usdt_cash = float(balance.get("total", {}).get("USDT", free_usdt) or free_usdt)

        open_positions = self.db.get_open_positions()
        tickers: Dict[str, Any] = {}
        if open_positions:
            try:
                tickers = await self.exchange.fetch_tickers()
            except Exception as exc:
                logger.warning("Reconciler failed to fetch tickers: %s", exc)

        positions_value_usd = 0.0
        reconciled_closed = 0
        tp_triggered = 0

        for pos in open_positions:
            symbol = pos["symbol"]
            pos_id = pos["position_id"]
            qty = float(pos["quantity"])
            sl_order_id = pos.get("sl_exchange_order_id")
            tp_price = float(pos["take_profit_price"])

            t = tickers.get(symbol, {})
            live_price = float(
                t.get("last") or t.get("close") or pos.get("current_price") or pos["entry_price"]
            )
            self.db.update_position_price(pos_id, live_price)

            # Check if Server-Side SL filled on Binance
            sl_filled = False
            if sl_order_id:
                try:
                    sl_info = await self.exchange.fetch_order(str(sl_order_id), symbol)
                    sl_status = str(sl_info.get("status") or "").lower()
                    if sl_status in ("closed", "filled"):
                        fill_p = float(
                            sl_info.get("average")
                            or sl_info.get("price")
                            or pos["invalidation_price"]
                        )
                        closed_p = self.db.close_position(
                            position_id=pos_id,
                            exit_price=fill_p,
                            close_reason="SERVER_SIDE_STOP_LOSS_FILLED",
                        )
                        reconciled_closed += 1
                        sl_filled = True
                        if self.telegram is not None and closed_p:
                            asyncio.create_task(
                                self.telegram.notify_exit(
                                    symbol=symbol,
                                    pos_id=pos_id,
                                    entry_price=float(closed_p.get("entry_price", pos.get("entry_price", 0))),
                                    exit_price=fill_p,
                                    qty=qty,
                                    realized_pnl=float(closed_p.get("realized_pnl_usd", 0.0)),
                                    reason="SERVER_SIDE_STOP_LOSS_FILLED",
                                )
                            )
                except Exception as exc:
                    logger.debug("Could not fetch SL order %s for %s: %s", sl_order_id, symbol, exc)

            if sl_filled:
                continue

            # Check if Take-Profit price reached
            if live_price >= tp_price and self.execution is not None:
                try:
                    await self.execution.close_single_position_market(
                        position_id=pos_id, reason="TAKE_PROFIT_HIT"
                    )
                    tp_triggered += 1
                    continue
                except Exception as exc:
                    logger.error("Failed to close position %s at Take-Profit: %s", pos_id, exc)

            positions_value_usd += qty * live_price

        total_equity_usd = total_usdt_cash + positions_value_usd
        prev_peak = float(self.db.get_state("peak_equity_usd", "0.0") or 0.0)
        peak_equity = max(prev_peak, total_equity_usd)
        drawdown_pct = (
            ((peak_equity - total_equity_usd) / peak_equity) * 100.0 if peak_equity > 0.0 else 0.0
        )
        daily_pnl = self.db.get_daily_realized_pnl()

        self.db.set_state("free_usdt", f"{free_usdt:.2f}")
        self.db.set_state("total_equity_usd", f"{total_equity_usd:.2f}")
        self.db.set_state("peak_equity_usd", f"{peak_equity:.2f}")
        self.db.set_state("current_drawdown_pct", f"{max(0.0, drawdown_pct):.2f}")
        self.db.set_state("daily_realized_pnl_usd", f"{daily_pnl:.2f}")

        if drawdown_pct >= float(self.cfg.MAX_DRAWDOWN_PCT) and not self.db.is_system_halted():
            self.db.set_system_halted(
                True, reason=f"Max Drawdown exceeded ({drawdown_pct:.2f}%)"
            )

        return {
            "free_usdt": round(free_usdt, 2),
            "total_equity_usd": round(total_equity_usd, 2),
            "drawdown_pct": round(max(0.0, drawdown_pct), 2),
            "daily_realized_pnl_usd": round(daily_pnl, 2),
            "reconciled_closed": reconciled_closed,
            "tp_triggered": tp_triggered,
        }
