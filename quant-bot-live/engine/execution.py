"""Idempotent CCXT Order Execution with Mandatory Server-Side Stop-Loss (STOP_LOSS_LIMIT) and Emergency Flatten."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, Optional

import ccxt.async_support as ccxt

from config.settings import Settings, settings
from database.db_manager import DatabaseManager
from engine.exchange import BinanceExchangeWrapper
from engine.pre_trade_analyzer import PreTradeAnalyzer
from engine.risk_engine import RiskEngine
from engine.schemas import ReflexDecisionSchema

logger = logging.getLogger(__name__)


class ExecutionEngine:
    """Executes spot or isolated futures long entries idempotently and places atomic Server-Side Stop-Loss orders."""

    def __init__(
        self,
        exchange: BinanceExchangeWrapper,
        db: DatabaseManager,
        risk_engine: RiskEngine,
        custom_settings: Optional[Settings] = None,
        telegram: Optional[Any] = None,
        pre_trade_analyzer: Optional[PreTradeAnalyzer] = None,
    ) -> None:
        self.exchange = exchange
        self.db = db
        self.risk_engine = risk_engine
        self.cfg = custom_settings or settings
        self.telegram = telegram
        self.pre_trade_analyzer = pre_trade_analyzer or PreTradeAnalyzer(
            db=db,
            exchange=exchange,
            telegram=telegram,
        )

    @staticmethod
    def build_client_order_id(symbol: str, causal_timestamp_ms: int, prefix: str = "QB") -> str:
        clean_sym = symbol.replace("/", "").replace("-", "").upper()
        return f"{prefix}_{clean_sym}_{int(causal_timestamp_ms)}"

    async def _place_order_idempotently(
        self,
        symbol: str,
        order_type: str,
        side: str,
        amount: float,
        price: Optional[float],
        client_order_id: str,
        extra_params: Optional[Dict[str, Any]] = None,
        max_attempts: int = 3,
    ) -> Dict[str, Any]:
        """
        Place an order with deterministic newClientOrderId.
        If create_order() raises RequestTimeout or NetworkError, query fetch_order_by_client_order_id() before retrying.
        """
        existing_db = self.db.get_order_by_client_id(client_order_id)
        if existing_db and existing_db.get("status") in ("closed", "open", "FILLED", "NEW"):
            logger.info("Duplicate prevented by local DB check for clientOrderId=%s", client_order_id)
            return existing_db

        params = dict(extra_params or {})
        params["newClientOrderId"] = client_order_id

        for attempt in range(1, max_attempts + 1):
            try:
                order = await self.exchange.create_order(
                    symbol=symbol,
                    order_type=order_type,
                    side=side,
                    amount=amount,
                    price=price,
                    params=params,
                )
                return order
            except (ccxt.RequestTimeout, ccxt.NetworkError) as exc:
                logger.warning(
                    "Timeout/NetworkError on %s (%s) attempt %d/%d: %s. Querying exchange by clientOrderId...",
                    symbol,
                    client_order_id,
                    attempt,
                    max_attempts,
                    exc,
                )
                found = await self.exchange.fetch_order_by_client_order_id(client_order_id, symbol)
                if found is not None:
                    logger.info(
                        "Recovered existing order on Binance for clientOrderId=%s (id=%s)",
                        client_order_id,
                        found.get("id"),
                    )
                    return found
                if attempt >= max_attempts:
                    raise
                await asyncio.sleep(0.25 * attempt)

        raise RuntimeError(f"Exhausted {max_attempts} attempts for {client_order_id}")

    async def execute_approved_entry(
        self,
        decision: ReflexDecisionSchema,
        free_usdt: float,
        futures_free_usdt: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        1. Auto-detect Dual Wallet mode: FUTURES (2x-3x Isolated) if futures_free_usdt >= $5.0, else SPOT.
        2. Pass decision through RiskEngine.evaluate_gate() (includes Daily Max 2 Trades cap).
        3. Pass through PreTradeAnalyzer (Orderbook Wall + Mistake Learner + 3-bullet Telegram briefing).
        4. Place idempotent entry order with QB_{SYMBOL}_{CAUSAL_TS} clientOrderId and Server-Side Stop-Loss.
        """
        await self.exchange.ensure_markets_loaded()
        min_notional = self.exchange.get_min_notional(decision.symbol, default_min_usd=10.0)

        if futures_free_usdt is None:
            futures_free_usdt = float(self.db.get_state("futures_free_usdt", "0.0") or 0.0)

        futures_min = float(getattr(self.cfg, "FUTURES_MIN_USDT", 5.0))
        futures_api_ok = self.db.get_state("futures_api_enabled", "true") != "false"
        effective_futures_usdt = float(futures_free_usdt) if futures_api_ok else 0.0
        trading_mode = "FUTURES" if effective_futures_usdt >= futures_min else "SPOT"

        gate = self.risk_engine.evaluate_gate(
            decision=decision,
            free_usdt=free_usdt,
            min_notional_usd=min_notional,
            trading_mode=trading_mode,
            futures_free_usdt=effective_futures_usdt,
        )
        if not gate["approved"]:
            self.db.save_reflex_decision(decision.model_dump(), executed=False)
            return {
                "executed": False,
                "reason": gate["reason"],
                "decision": decision.model_dump(),
            }

        position_usd = float(gate["position_usd"])
        leverage = int(gate.get("leverage", 1))
        trading_mode = str(gate.get("trading_mode", trading_mode))

        # Step 3: Pre-Trade Deep Analysis Gate (Orderbook Imbalance + Mistake Learner + 3-bullet Telegram summary)
        pre_trade_msg: Optional[str] = None
        if self.pre_trade_analyzer is not None:
            pre_check = await self.pre_trade_analyzer.analyze_before_execution(
                decision=decision,
                trading_mode=trading_mode,
                position_usd=position_usd,
                leverage=leverage,
                min_confidence=self.risk_engine.get_active_min_confidence(),
                send_telegram=False,
            )
            if not pre_check.get("approved", True):
                self.db.save_reflex_decision(decision.model_dump(), executed=False)
                return {
                    "executed": False,
                    "reason": pre_check.get("reason", "Pre-Trade Gate rejected"),
                    "decision": decision.model_dump(),
                }
            pre_trade_msg = pre_check.get("telegram_message")

        raw_qty = position_usd / max(decision.entry_price, 1e-9)
        qty = self.exchange.amount_to_precision(decision.symbol, raw_qty)
        is_live_ccxt = getattr(self.exchange.client, "urls", None) is not None
        limit_buy_price = decision.entry_price * 1.0015 if is_live_ccxt else decision.entry_price
        entry_price_fmt = self.exchange.price_to_precision(decision.symbol, limit_buy_price)

        if qty <= 0.0:
            return {
                "executed": False,
                "reason": f"Formatted quantity is 0 for {decision.symbol}",
            }

        entry_client_id = self.build_client_order_id(
            decision.symbol, decision.causal_timestamp_ms, prefix="QB"
        )

        # Configure Isolated Margin & Leverage if running in FUTURES mode
        use_futures_client = (
            trading_mode == "FUTURES"
            and getattr(self.exchange, "futures_client", None) is not None
        )
        entry_order: Optional[Dict[str, Any]] = None
        try:
            if use_futures_client:
                try:
                    actual_lev = await self.exchange.configure_futures_isolated(
                        decision.symbol, leverage=leverage
                    )
                    if actual_lev and actual_lev != leverage:
                        position_usd = round((position_usd / max(leverage, 1)) * actual_lev, 2)
                        leverage = int(actual_lev)
                        qty = self.exchange.amount_to_precision(
                            decision.symbol, position_usd / max(decision.entry_price, 1e-9)
                        )
                    entry_order = await self.exchange.create_futures_order(
                        symbol=decision.symbol,
                        order_type="limit",
                        side="buy",
                        amount=qty,
                        price=entry_price_fmt,
                        params={"newClientOrderId": entry_client_id, "timeInForce": "GTC"},
                    )
                except Exception as fut_exc:
                    logger.warning(
                        "Futures order failed on %s (%s); checking Spot fallback...",
                        decision.symbol,
                        fut_exc,
                    )
                    if float(free_usdt) >= 11.50:
                        use_futures_client = False
                        trading_mode = "SPOT"
                        leverage = 1
                        position_usd = 11.50
                        qty = self.exchange.amount_to_precision(
                            decision.symbol, position_usd / max(decision.entry_price, 1e-9)
                        )
                        entry_client_id = f"{entry_client_id}_S"
                        if pre_trade_msg:
                            pre_trade_msg = pre_trade_msg.replace("FUTURES 2x ISOLATED", "SPOT").replace(
                                "FUTURES 3x ISOLATED", "SPOT"
                            )
                        entry_order = await self._place_order_idempotently(
                            symbol=decision.symbol,
                            order_type="limit",
                            side="buy",
                            amount=qty,
                            price=entry_price_fmt,
                            client_order_id=entry_client_id,
                            max_attempts=2,
                        )
                    else:
                        raise fut_exc
            else:
                # Place Spot entry order idempotently
                entry_order = await self._place_order_idempotently(
                    symbol=decision.symbol,
                    order_type="limit",
                    side="buy",
                    amount=qty,
                    price=entry_price_fmt,
                    client_order_id=entry_client_id,
                    max_attempts=3,
                )
        except Exception as order_exc:
            err_str = str(order_exc)
            if "-2015" in err_str or "permissions for action" in err_str:
                now_s = int(time.time())
                last_alert_s = int(self.db.get_state("last_perm_alert_s", "0") or 0)
                if self.telegram is not None and (now_s - last_alert_s) >= 1800:
                    self.db.set_state("last_perm_alert_s", str(now_s))
                    await self.telegram.send_message(
                        "⚠️ *BINANCE API İZİN HATASI (-2015)*\n\n"
                        f"`{decision.symbol}` için giriş sinyali yakalandı ancak Binance emri reddetti:\n"
                        "`Invalid API-key, IP, or permissions for action (-2015)`\n\n"
                        "🔧 *Çözüm (1 Dakika):*\n"
                        "1. Binance -> **API Management** sayfasına girin.\n"
                        "2. **Edit restrictions**'a tıklayın.\n"
                        "3. **Restrict access to trusted IPs only** seçeneğini işaretleyip sunucu IP'miz olan **`34.17.158.26`** adresini ekleyin.\n"
                        "4. **Enable Spot & Margin Trading** ve **Enable Futures** kutucuklarını işaretleyip **Save** yapın."
                    )
            raise

        if pre_trade_msg and self.telegram is not None:
            await self.telegram.send_message(pre_trade_msg)

        exchange_order_id = str(entry_order.get("id") or entry_order.get("exchange_order_id") or "")
        filled_qty = float(entry_order.get("filled") or entry_order.get("amount") or qty)
        fill_price = float(
            entry_order.get("average") or entry_order.get("price") or entry_price_fmt
        )
        cost_usd = float(entry_order.get("cost") or (filled_qty * fill_price))

        self.db.upsert_order(
            {
                "client_order_id": entry_client_id,
                "exchange_order_id": exchange_order_id,
                "symbol": decision.symbol,
                "side": "buy",
                "order_type": "limit",
                "status": str(entry_order.get("status") or "closed"),
                "price": fill_price,
                "stop_price": None,
                "amount": qty,
                "filled": filled_qty,
                "cost_usd": cost_usd,
                "causal_timestamp_ms": decision.causal_timestamp_ms,
            }
        )

        # Determine net fillable token balance after Binance spot fee deduction
        base_asset = decision.symbol.split("/")[0]
        try:
            balance = await self.exchange.fetch_balance()
            free_base = float(balance.get("free", {}).get(base_asset, 0.0) or 0.0)
            sl_raw_qty = min(filled_qty, free_base) if free_base > 0.0 else filled_qty * 0.999
        except Exception:
            sl_raw_qty = filled_qty * 0.999

        sl_qty = self.exchange.amount_to_precision(decision.symbol, sl_raw_qty)
        if sl_qty <= 0.0:
            sl_qty = self.exchange.amount_to_precision(decision.symbol, filled_qty)

        stop_price = self.exchange.price_to_precision(decision.symbol, decision.invalidation_price)
        limit_sl_price = self.exchange.price_to_precision(
            decision.symbol, decision.invalidation_price * 0.997
        )
        sl_client_id = self.build_client_order_id(
            decision.symbol, decision.causal_timestamp_ms, prefix="QBSL"
        )

        # Place mandatory Server-Side STOP_LOSS_LIMIT within 500ms (up to 3 attempts)
        sl_order: Optional[Dict[str, Any]] = None
        sl_error: Optional[Exception] = None
        for sl_attempt in range(1, 4):
            try:
                if use_futures_client:
                    sl_order = await self.exchange.create_futures_order(
                        symbol=decision.symbol,
                        order_type="STOP",
                        side="sell",
                        amount=sl_qty,
                        price=limit_sl_price,
                        params={
                            "stopPrice": stop_price,
                            "reduceOnly": True,
                            "newClientOrderId": sl_client_id,
                        },
                    )
                else:
                    sl_order = await self.exchange.create_order(
                        symbol=decision.symbol,
                        order_type="STOP_LOSS_LIMIT",
                        side="sell",
                        amount=sl_qty,
                        price=limit_sl_price,
                        params={
                            "stopPrice": stop_price,
                            "timeInForce": "GTC",
                            "newClientOrderId": sl_client_id,
                        },
                    )
                break
            except Exception as exc:
                sl_error = exc
                logger.error(
                    "Server-Side Stop-Loss attempt %d/3 failed for %s: %s",
                    sl_attempt,
                    decision.symbol,
                    exc,
                )
                if sl_attempt < 3:
                    await asyncio.sleep(0.1)

        if sl_order is None:
            # EMERGENCY FLATTEN RULE: Immediately market-sell unprotected position and pause symbol for 4h
            logger.critical(
                "CRITICAL: Server-Side Stop-Loss failed after 3 attempts on %s (%s). "
                "Executing immediate market sell to eliminate unprotected exposure!",
                decision.symbol,
                sl_error,
            )
            emergency_sell = await self.exchange.create_market_sell_order(decision.symbol, sl_qty)
            self.db.pause_symbol(decision.symbol, duration_seconds=4 * 3600)
            self.db.save_reflex_decision(decision.model_dump(), executed=False)
            return {
                "executed": False,
                "emergency_flattened": True,
                "symbol": decision.symbol,
                "reason": f"SL placement failed after 3 retries ({sl_error}); position immediately market-sold.",
                "emergency_sell_order": emergency_sell,
            }

        sl_exchange_id = str(sl_order.get("id") or "")
        self.db.upsert_order(
            {
                "client_order_id": sl_client_id,
                "exchange_order_id": sl_exchange_id,
                "symbol": decision.symbol,
                "side": "sell",
                "order_type": "STOP_LOSS_LIMIT",
                "status": str(sl_order.get("status") or "open"),
                "price": limit_sl_price,
                "stop_price": stop_price,
                "amount": sl_qty,
                "filled": float(sl_order.get("filled") or 0.0),
                "cost_usd": 0.0,
                "causal_timestamp_ms": decision.causal_timestamp_ms,
            }
        )

        position_id = f"POS_{decision.symbol.replace('/', '')}_{decision.causal_timestamp_ms}"
        self.db.open_position(
            {
                "position_id": position_id,
                "symbol": decision.symbol,
                "entry_client_order_id": entry_client_id,
                "entry_exchange_order_id": exchange_order_id,
                "sl_client_order_id": sl_client_id,
                "sl_exchange_order_id": sl_exchange_id,
                "entry_price": fill_price,
                "current_price": fill_price,
                "invalidation_price": stop_price,
                "take_profit_price": decision.take_profit_price,
                "quantity": sl_qty,
                "cost_usd": cost_usd,
                "direction_confidence": decision.direction_confidence,
                "setup_quality": decision.setup_quality,
                "regime": decision.regime,
                "opened_at_ms": int(time.time() * 1000),
            }
        )
        self.db.save_reflex_decision(decision.model_dump(), executed=True)

        if self.telegram is not None:
            asyncio.create_task(
                self.telegram.notify_entry(
                    symbol=decision.symbol,
                    pos_id=position_id,
                    entry_price=fill_price,
                    qty=sl_qty,
                    cost_usd=cost_usd,
                    invalidation_price=stop_price,
                    take_profit_price=decision.take_profit_price,
                    confidence=decision.direction_confidence,
                    regime=f"{decision.regime} ({trading_mode}{f' {leverage}x' if leverage > 1 else ''})",
                )
            )

        return {
            "executed": True,
            "emergency_flattened": False,
            "position_id": position_id,
            "trading_mode": trading_mode,
            "leverage": leverage,
            "entry_order": entry_order,
            "sl_order": sl_order,
            "position_usd": position_usd,
            "quantity": sl_qty,
        }

    async def close_single_position_market(
        self, position_id: str, reason: str = "MANUAL_MARKET_CLOSE"
    ) -> Dict[str, Any]:
        """Cancel open Server-Side SL order for a specific position and market-sell its tokens."""
        open_positions = self.db.get_open_positions()
        target = next((p for p in open_positions if p["position_id"] == position_id), None)
        if target is None:
            return {"closed": False, "reason": f"Open position {position_id} not found."}

        symbol = target["symbol"]
        qty = float(target["quantity"])
        sl_order_id = target.get("sl_exchange_order_id")

        if sl_order_id:
            try:
                await self.exchange.cancel_order(str(sl_order_id), symbol)
            except Exception as exc:
                logger.warning("Could not cancel SL order %s for %s: %s", sl_order_id, symbol, exc)

        sell_order = await self.exchange.create_market_sell_order(symbol, qty)
        exit_price = float(
            sell_order.get("average") or sell_order.get("price") or target["current_price"]
        )
        closed_pos = self.db.close_position(
            position_id=position_id,
            exit_price=exit_price,
            close_reason=reason,
        )
        if self.telegram is not None and closed_pos:
            asyncio.create_task(
                self.telegram.notify_exit(
                    symbol=symbol,
                    pos_id=position_id,
                    entry_price=float(closed_pos.get("entry_price", target.get("entry_price", 0))),
                    exit_price=exit_price,
                    qty=qty,
                    realized_pnl=float(closed_pos.get("realized_pnl_usd", 0.0)),
                    reason=reason,
                )
            )
        return {
            "closed": True,
            "position": closed_pos,
            "sell_order": sell_order,
        }
