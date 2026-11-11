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
from engine.task_registry import TaskRegistry

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
        self.task_registry = TaskRegistry()

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
        if not self.task_registry.accepting_entries:
            logger.warning("Rejecting entry for %s: Engine shutdown in progress.", decision.symbol)
            return {"executed": False, "reason": "SHUTDOWN_IN_PROGRESS", "decision": decision.model_dump()}

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

        # === T0: PAPER TRADING SIMULATION PATH ===
        if getattr(self.cfg, "is_paper", True):
            logger.info("Executing paper trade for %s in %s mode (leverage=%dx)", decision.symbol, trading_mode, leverage)
            fill_price = float(entry_price_fmt)
            filled_qty = qty
            cost_usd = round(filled_qty * fill_price, 4)
            margin_usd = round(cost_usd / max(leverage, 1), 4)
            exchange_order_id = f"PAPER_ENTRY_{decision.causal_timestamp_ms}"
            sl_client_id = self.build_client_order_id(decision.symbol, decision.causal_timestamp_ms, prefix="QBSL")
            sl_exchange_id = f"PAPER_SL_{decision.causal_timestamp_ms}"
            stop_price = self.exchange.price_to_precision(decision.symbol, decision.invalidation_price)
            limit_sl_price = self.exchange.price_to_precision(decision.symbol, decision.invalidation_price * 0.997)

            self.db.upsert_order(
                {
                    "client_order_id": entry_client_id,
                    "exchange_order_id": exchange_order_id,
                    "symbol": decision.symbol,
                    "side": "buy",
                    "order_type": "limit",
                    "status": "closed",
                    "price": fill_price,
                    "stop_price": None,
                    "amount": qty,
                    "filled": filled_qty,
                    "cost_usd": cost_usd,
                    "causal_timestamp_ms": decision.causal_timestamp_ms,
                }
            )
            self.db.upsert_order(
                {
                    "client_order_id": sl_client_id,
                    "exchange_order_id": sl_exchange_id,
                    "symbol": decision.symbol,
                    "side": "sell",
                    "order_type": "STOP_LOSS_LIMIT",
                    "status": "open",
                    "price": float(limit_sl_price),
                    "stop_price": float(stop_price),
                    "amount": filled_qty,
                    "filled": 0.0,
                    "cost_usd": 0.0,
                    "causal_timestamp_ms": decision.causal_timestamp_ms,
                }
            )

            position_id = f"POS_{decision.symbol.replace('/', '')}_{decision.causal_timestamp_ms}"
            self.db.open_position(
                {
                    "position_id": position_id,
                    "symbol": decision.symbol,
                    "status": "OPEN",
                    "entry_client_order_id": entry_client_id,
                    "entry_exchange_order_id": exchange_order_id,
                    "sl_client_order_id": sl_client_id,
                    "sl_exchange_order_id": sl_exchange_id,
                    "entry_price": fill_price,
                    "current_price": fill_price,
                    "invalidation_price": float(stop_price),
                    "take_profit_price": decision.take_profit_price,
                    "quantity": filled_qty,
                    "cost_usd": cost_usd,
                    "direction_confidence": decision.direction_confidence,
                    "setup_quality": decision.setup_quality,
                    "regime": decision.regime,
                    "opened_at_ms": int(time.time() * 1000),
                    "market_type": trading_mode.lower(),
                    "leverage": leverage,
                    "margin_usd": margin_usd,
                    "notional_usd": cost_usd,
                    "sl_slippage_pct": 0.0,
                    "is_paper": True,
                }
            )
            self.db.save_reflex_decision(decision.model_dump(), executed=True)

            # Record cash deduction in paper ledger
            if trading_mode.lower() == "futures":
                fee_rate = getattr(self.cfg, "FUTURES_FEE_PCT", 0.05) / 100.0
                fee_usd = cost_usd * fee_rate
                deduction = margin_usd + fee_usd
                self.db.record_paper_ledger(
                    event="ENTRY_BUY_FUTURES",
                    amount=-deduction,
                    symbol=decision.symbol,
                    details={"cost_usd": cost_usd, "margin_usd": margin_usd, "fee_usd": fee_usd, "leverage": leverage},
                )
            else:
                fee_rate = getattr(self.cfg, "SPOT_FEE_PCT", 0.10) / 100.0
                fee_usd = cost_usd * fee_rate
                deduction = cost_usd + fee_usd
                self.db.record_paper_ledger(
                    event="ENTRY_BUY_SPOT",
                    amount=-deduction,
                    symbol=decision.symbol,
                    details={"cost_usd": cost_usd, "fee_usd": fee_usd, "price": fill_price, "qty": filled_qty},
                )

            if self.telegram is not None:
                asyncio.create_task(
                    self.telegram.notify_entry(
                        symbol=decision.symbol,
                        pos_id=position_id,
                        entry_price=fill_price,
                        qty=filled_qty,
                        cost_usd=cost_usd,
                        invalidation_price=float(stop_price),
                        take_profit_price=decision.take_profit_price,
                        confidence=decision.direction_confidence,
                        regime=f"{decision.regime} (PAPER {trading_mode}{f' {leverage}x' if leverage > 1 else ''})",
                    )
                )

            return {
                "executed": True,
                "emergency_flattened": False,
                "position_id": position_id,
                "trading_mode": trading_mode,
                "leverage": leverage,
                "entry_order": {"id": exchange_order_id, "clientOrderId": entry_client_id, "status": "closed", "filled": filled_qty, "price": fill_price},
                "sl_order": {"id": sl_exchange_id, "clientOrderId": sl_client_id, "status": "open", "price": float(limit_sl_price), "stopPrice": float(stop_price)},
                "position_usd": position_usd,
                "quantity": filled_qty,
                "is_paper": True,
            }

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
                    qty = self.exchange.futures_amount_to_precision(
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
                        "3. **Restrict access to trusted IPs only** seçeneğini işaretleyip sunucu IP adresinizi ekleyin.\n"
                        "4. **Enable Spot & Margin Trading** ve **Enable Futures** kutucuklarını işaretleyip **Save** yapın."
                    )
            raise

        if pre_trade_msg and self.telegram is not None:
            await self.telegram.send_message(pre_trade_msg)

        exchange_order_id = str(entry_order.get("id") or entry_order.get("exchange_order_id") or "")
        raw_filled = entry_order.get("filled")
        filled_qty = float(raw_filled or 0.0)
        order_status = str(entry_order.get("status") or "closed").lower()

        # === T1: ENTRY TTL & POLL LOOP ===
        ttl_sec = float(getattr(self.cfg, "ENTRY_ORDER_TTL_SEC", 90.0))
        poll_interval_sec = float(getattr(self.cfg, "ENTRY_ORDER_POLL_SEC", 2.0))

        sl_order: Optional[Dict[str, Any]] = None
        sl_interim_qty: float = 0.0

        if is_live_ccxt and (filled_qty < qty or order_status == "open"):
            start_poll_time = time.time()
            while time.time() - start_poll_time < ttl_sec:
                await asyncio.sleep(poll_interval_sec)
                try:
                    refreshed = (
                        await self.exchange.fetch_futures_order(exchange_order_id, decision.symbol)
                        if use_futures_client and hasattr(self.exchange, "fetch_futures_order")
                        else await self.exchange.fetch_order(exchange_order_id, decision.symbol)
                    )
                    if refreshed:
                        entry_order = refreshed
                        filled_qty = float(entry_order.get("filled") or 0.0)
                        order_status = str(entry_order.get("status") or order_status).lower()

                        # Immediate SL placement for partial fills during poll loop (<=5 sec response)
                        if filled_qty > 0.0 and sl_order is None:
                            try:
                                poll_stop_p = (
                                    self.exchange.futures_price_to_precision(decision.symbol, decision.invalidation_price)
                                    if use_futures_client
                                    else self.exchange.price_to_precision(decision.symbol, decision.invalidation_price)
                                )
                                poll_limit_p = (
                                    self.exchange.futures_price_to_precision(decision.symbol, decision.invalidation_price * 0.997)
                                    if use_futures_client
                                    else self.exchange.price_to_precision(decision.symbol, decision.invalidation_price * 0.997)
                                )
                                poll_sl_qty = (
                                    self.exchange.futures_amount_to_precision(decision.symbol, filled_qty)
                                    if use_futures_client
                                    else self.exchange.amount_to_precision(decision.symbol, filled_qty * 0.999)
                                )
                                poll_client_id = self.build_client_order_id(decision.symbol, decision.causal_timestamp_ms, prefix="QBSL")
                                if use_futures_client:
                                    sl_order = await self.exchange.create_futures_order(
                                        symbol=decision.symbol,
                                        order_type="STOP",
                                        side="sell",
                                        amount=poll_sl_qty,
                                        price=poll_limit_p,
                                        params={"stopPrice": poll_stop_p, "reduceOnly": True, "newClientOrderId": poll_client_id},
                                    )
                                else:
                                    sl_order = await self.exchange.create_order(
                                        symbol=decision.symbol,
                                        order_type="STOP_LOSS_LIMIT",
                                        side="sell",
                                        amount=poll_sl_qty,
                                        price=poll_limit_p,
                                        params={"stopPrice": poll_stop_p, "timeInForce": "GTC", "newClientOrderId": poll_client_id},
                                    )
                                sl_interim_qty = poll_sl_qty
                                logger.info("Immediate partial-fill SL placed during poll loop for %s: qty=%.6f", decision.symbol, poll_sl_qty)
                            except Exception as sl_poll_err:
                                logger.warning("Immediate partial SL placement warning for %s: %s", decision.symbol, sl_poll_err)

                        if order_status in ("closed", "canceled", "rejected", "expired") or filled_qty >= qty:
                            break
                except Exception as poll_exc:
                    logger.debug("Transient error polling entry order %s: %s", exchange_order_id, poll_exc)

            # If still open after TTL or early exit, cancel the remaining unfilled balance
            if order_status == "open":
                try:
                    if use_futures_client and getattr(self.exchange, "futures_client", None) is not None:
                        fut_sym = self.exchange.resolve_futures_symbol(decision.symbol)
                        await self.exchange.futures_client.cancel_order(exchange_order_id, fut_sym)
                    else:
                        await self.exchange.cancel_order(exchange_order_id, decision.symbol)
                    order_status = "canceled" if filled_qty <= 0.0 else "closed"
                except Exception as cancel_exc:
                    logger.warning("Could not cancel remaining open entry order %s: %s", exchange_order_id, cancel_exc)

                # Final refresh after cancellation to get exact filled quantity
                try:
                    final_check = (
                        await self.exchange.fetch_futures_order(exchange_order_id, decision.symbol)
                        if use_futures_client and hasattr(self.exchange, "fetch_futures_order")
                        else await self.exchange.fetch_order(exchange_order_id, decision.symbol)
                    )
                    if final_check:
                        entry_order = final_check
                        filled_qty = float(entry_order.get("filled") or filled_qty)
                        order_status = str(entry_order.get("status") or order_status).lower()
                except Exception as final_exc:
                    logger.debug("Could not run final fetch for order %s: %s", exchange_order_id, final_exc)

            if filled_qty <= 0.0:
                self.db.upsert_order(
                    {
                        "client_order_id": entry_client_id,
                        "exchange_order_id": exchange_order_id,
                        "symbol": decision.symbol,
                        "side": "buy",
                        "order_type": "limit",
                        "status": "canceled",
                        "price": float(entry_order.get("price") or entry_price_fmt),
                        "stop_price": None,
                        "amount": qty,
                        "filled": 0.0,
                        "cost_usd": 0.0,
                        "causal_timestamp_ms": decision.causal_timestamp_ms,
                    }
                )
                return {
                    "executed": False,
                    "reason": f"Limit buy order {exchange_order_id} expired after TTL ({ttl_sec}s) with 0 fill and was canceled.",
                }

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
                "status": "closed" if filled_qty > 0.0 else order_status,
                "price": fill_price,
                "stop_price": None,
                "amount": qty,
                "filled": filled_qty,
                "cost_usd": cost_usd,
                "causal_timestamp_ms": decision.causal_timestamp_ms,
            }
        )

        # Determine net fillable token balance (on Spot deduct fee / check free token; on Futures use exact contract qty)
        if use_futures_client:
            sl_qty = self.exchange.futures_amount_to_precision(decision.symbol, filled_qty)
            stop_price = self.exchange.futures_price_to_precision(
                decision.symbol, decision.invalidation_price
            )
            limit_sl_price = self.exchange.futures_price_to_precision(
                decision.symbol, decision.invalidation_price * 0.997
            )
        else:
            base_asset = decision.symbol.split("/")[0]
            try:
                balance = await self.exchange.fetch_balance()
                free_base = float(balance.get("free", {}).get(base_asset, 0.0) or 0.0)
                sl_raw_qty = (
                    min(filled_qty, free_base)
                    if free_base >= filled_qty * 0.50
                    else filled_qty * 0.999
                )
            except Exception:
                sl_raw_qty = filled_qty * 0.999

            sl_qty = self.exchange.amount_to_precision(decision.symbol, sl_raw_qty)
            if sl_qty <= 0.0:
                sl_qty = self.exchange.amount_to_precision(decision.symbol, filled_qty)

            stop_price = self.exchange.price_to_precision(
                decision.symbol, decision.invalidation_price
            )
            limit_sl_price = self.exchange.price_to_precision(
                decision.symbol, decision.invalidation_price * 0.997
            )
        sl_client_id = self.build_client_order_id(
            decision.symbol, decision.causal_timestamp_ms, prefix="QBSL"
        )

        # Place mandatory Server-Side STOP_LOSS_LIMIT within 500ms (up to 3 attempts)
        sl_error: Optional[Exception] = None
        if sl_order is not None:
            if abs(sl_interim_qty - sl_qty) < 1e-8:
                # Interim SL placed during poll already covers exact final filled quantity
                logger.debug("Reusing immediate poll SL %s for %s", sl_order.get("id"), decision.symbol)
            else:
                # Cancel smaller interim SL to re-arm with complete final filled amount
                try:
                    interim_id = str(sl_order.get("id") or "")
                    if use_futures_client and getattr(self.exchange, "futures_client", None) is not None:
                        fut_sym = self.exchange.resolve_futures_symbol(decision.symbol)
                        await self.exchange.futures_client.cancel_order(interim_id, fut_sym, params={"stop": True})
                    else:
                        await self.exchange.cancel_order(interim_id, decision.symbol)
                except Exception:
                    pass
                sl_order = None

        if sl_order is None:
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
            # EMERGENCY FLATTEN RULE: Mark position as OPEN_UNPROTECTED first so state reconciler knows it's exposed
            unprot_pos_id = f"POS_{decision.symbol.replace('/', '')}_{decision.causal_timestamp_ms}"
            self.db.open_position(
                {
                    "position_id": unprot_pos_id,
                    "symbol": decision.symbol,
                    "status": "OPEN_UNPROTECTED",
                    "entry_client_order_id": entry_client_id,
                    "entry_exchange_order_id": exchange_order_id,
                    "sl_client_order_id": None,
                    "sl_exchange_order_id": None,
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
                    "market_type": trading_mode.lower(),
                    "leverage": leverage,
                    "margin_usd": round(cost_usd / max(leverage, 1), 4) if use_futures_client else cost_usd,
                    "notional_usd": cost_usd,
                    "sl_slippage_pct": 0.0,
                    "is_paper": False,
                }
            )

            logger.critical(
                "CRITICAL: Server-Side Stop-Loss failed after 3 attempts on %s (%s). "
                "Executing immediate market sell to eliminate unprotected exposure!",
                decision.symbol,
                sl_error,
            )
            emergency_sell = None
            try:
                if use_futures_client:
                    emergency_sell = await self.exchange.create_futures_order(
                        symbol=decision.symbol,
                        order_type="market",
                        side="sell",
                        amount=sl_qty,
                        price=None,
                        params={"reduceOnly": True},
                    )
                else:
                    emergency_sell = await self.exchange.create_market_sell_order(decision.symbol, sl_qty)
                
                # If market sell succeeded, close the position cleanly
                exit_price = float((emergency_sell or {}).get("average") or (emergency_sell or {}).get("price") or fill_price)
                self.db.close_position(
                    position_id=unprot_pos_id,
                    exit_price=exit_price,
                    close_reason="EMERGENCY_FLATTEN_SL_PLACEMENT_FAILED",
                )
            except Exception as em_exc:
                logger.critical("EMERGENCY FLATTEN SELL FAILED on %s: %s! Position remains in OPEN_UNPROTECTED status!", decision.symbol, em_exc)

            self.db.pause_symbol(decision.symbol, duration_seconds=4 * 3600)
            self.db.save_reflex_decision(decision.model_dump(), executed=False)
            return {
                "executed": False,
                "emergency_flattened": True,
                "symbol": decision.symbol,
                "reason": f"SL placement failed after 3 retries ({sl_error}); position market-sold.",
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
        margin_usd = round(cost_usd / max(leverage, 1), 4) if use_futures_client else cost_usd
        self.db.open_position(
            {
                "position_id": position_id,
                "symbol": decision.symbol,
                "status": "OPEN",
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
                "market_type": trading_mode.lower(),
                "leverage": leverage,
                "margin_usd": margin_usd,
                "notional_usd": cost_usd,
                "sl_slippage_pct": 0.0,
                "is_paper": False,
            }
        )
        self.db.save_reflex_decision(decision.model_dump(), executed=True)

        # R12: Record entry-time features for pristine mistake learning
        try:
            snap = self.db.get_latest_state_snapshot(decision.symbol) or {}
            self.db.save_trade_features(
                {
                    "position_id": position_id,
                    "strategy_id": str(getattr(decision, "strategy_id", None) or decision.regime),
                    "regime": decision.regime,
                    "rsi": float(snap.get("rsi_14", 50.0)),
                    "prev_rsi": float(snap.get("prev_rsi_14", 50.0)),
                    "adx": float(snap.get("adx_14", 20.0)),
                    "atr_pct": float(snap.get("atr_14", 1.0)) / max(fill_price, 1e-9) * 100.0,
                    "bb_dist_pct": (fill_price - float(snap.get("bb_lower", fill_price))) / max(fill_price, 1e-9) * 100.0,
                    "ema_dist_pct": (fill_price - float(snap.get("ema_21", fill_price))) / max(fill_price, 1e-9) * 100.0,
                    "vol_ratio": float(snap.get("closed_volume", 1.0)) / max(float(snap.get("vol_sma_20", 1.0)), 1e-9),
                    "ob_imbalance": float(snap.get("orderbook_imbalance", 0.0)),
                    "spread_bps": float(snap.get("spread_bps", 0.0)),
                    "btc_trend_state": str(self.db.get_state("macro_regime", "neutral")),
                    "hour_utc": int(time.gmtime().tm_hour),
                    "side": str(decision.direction),
                    "leverage": leverage,
                }
            )
        except Exception as tf_exc:
            logger.debug("Could not record trade features for %s: %s", position_id, tf_exc)

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
        """Cancel open Server-Side SL order for a specific position and market-sell its tokens/contracts."""
        open_positions = self.db.get_open_positions(include_unprotected=True)
        target = next((p for p in open_positions if p["position_id"] == position_id), None)
        if target is None:
            return {"closed": False, "reason": f"Open position {position_id} not found."}

        symbol = target["symbol"]
        qty = float(target["quantity"])
        sl_order_id = target.get("sl_exchange_order_id")

        # In paper mode, do not send any close orders to Binance
        if target.get("is_paper") or getattr(self.cfg, "is_paper", True):
            exit_price = float(target.get("current_price") or target["entry_price"])
            now_ms = int(time.time() * 1000)
            self.db.upsert_order({
                "client_order_id": f"PAPER_CLOSE_{position_id}_{now_ms}",
                "exchange_order_id": None,
                "symbol": symbol,
                "side": "sell",
                "order_type": "market",
                "status": "closed",
                "price": exit_price,
                "stop_price": None,
                "amount": qty,
                "filled": qty,
                "cost_usd": exit_price * qty,
                "causal_timestamp_ms": now_ms,
                "created_at_ms": now_ms,
                "updated_at_ms": now_ms,
            })
            closed_pos = self.db.close_position(
                position_id=position_id,
                exit_price=exit_price,
                close_reason=reason,
                pnl_estimated=False,
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
                        reason=f"PAPER_{reason}",
                    )
                )
            return {"closed": True, "position_id": position_id, "exit_price": exit_price, "is_paper": True}

        fut_positions = {}
        if hasattr(self.exchange, "fetch_active_futures_positions"):
            fut_positions = await self.exchange.fetch_active_futures_positions()
        fut_pos = fut_positions.get(symbol)

        sell_order: Dict[str, Any] = {}
        if fut_pos is not None and getattr(self.exchange, "futures_client", None) is not None:
            fut_sym = self.exchange.resolve_futures_symbol(symbol)
            contracts = abs(float(fut_pos.get("contracts") or qty))
            if contracts > 0:
                # 1. Futures: ReduceOnly market sell FIRST to lock in exit
                sell_order = await self.exchange.create_futures_order(
                    symbol=symbol,
                    order_type="market",
                    side="sell",
                    amount=contracts,
                    price=None,
                    params={"reduceOnly": True},
                )
                # 2. Cancel SL only after market sell order is submitted
                if sl_order_id:
                    try:
                        await self.exchange.futures_client.cancel_order(
                            str(sl_order_id), fut_sym, params={"stop": True}
                        )
                    except Exception:
                        try:
                            await self.exchange.futures_client.cancel_order(str(sl_order_id), fut_sym)
                        except Exception:
                            pass
        else:
            # Spot: Must cancel SL first to release locked balance
            if sl_order_id:
                try:
                    await self.exchange.cancel_order(str(sl_order_id), symbol)
                except Exception as exc:
                    logger.warning("Could not cancel SL order %s for %s: %s", sl_order_id, symbol, exc)
            try:
                sell_order = await self.exchange.create_market_sell_order(symbol, qty)
            except Exception as exc:
                logger.critical("Market sell FAILED on %s: %s! RE-ARMING Stop-Loss immediately!", symbol, exc)
                # MUST RE-ARM SL so position is not left unprotected
                rearm_sl = None
                try:
                    inval_p = float(target["invalidation_price"])
                    limit_p = self.exchange.price_to_precision(symbol, inval_p * 0.997)
                    stop_p = self.exchange.price_to_precision(symbol, inval_p)
                    rearm_id = f"QBSL_REARM_{int(time.time() * 1000)}"
                    rearm_sl = await self.exchange.create_order(
                        symbol=symbol,
                        order_type="STOP_LOSS_LIMIT",
                        side="sell",
                        amount=qty,
                        price=limit_p,
                        params={"stopPrice": stop_p, "timeInForce": "GTC", "newClientOrderId": rearm_id},
                    )
                    if rearm_sl and rearm_sl.get("id"):
                        self.db.open_position({
                            **target,
                            "sl_exchange_order_id": str(rearm_sl["id"]),
                            "sl_client_order_id": rearm_id,
                        })
                except Exception as rearm_exc:
                    logger.critical("FATAL: Failed to rearm SL on %s: %s!", symbol, rearm_exc)

                # DO NOT mark closed in DB!
                return {
                    "closed": False,
                    "position_id": position_id,
                    "reason": f"Market sell failed ({exc}); SL rearmed: {rearm_sl is not None}",
                }

        # S5: Compute realized PnL using real exchange fills and commissions
        realized_pnl = None
        pnl_estimated = False
        matching_trades = []

        if sell_order.get("id") and hasattr(self.exchange, "client") and hasattr(self.exchange.client, "fetch_my_trades"):
            try:
                trades = await self.exchange.client.fetch_my_trades(symbol, since=int(target.get("opened_at_ms") or 0))
                matching_trades = [t for t in trades if str(t.get("order")) == str(sell_order["id"])]
            except Exception as trade_exc:
                logger.debug("Could not fetch trades for close order %s: %s", sell_order.get("id"), trade_exc)

        if matching_trades:
            total_amt = sum(float(t.get("amount") or 0.0) for t in matching_trades)
            if total_amt > 0.0:
                exit_price = sum(float(t.get("price") or 0.0) * float(t.get("amount") or 0.0) for t in matching_trades) / total_amt
                fee_usd = sum(float((t.get("fee") or {}).get("cost") or 0.0) for t in matching_trades)
                realized_pnl = (exit_price - float(target["entry_price"])) * qty - fee_usd
                pnl_estimated = False
        elif sell_order.get("average") or sell_order.get("price"):
            exit_price = float(sell_order.get("average") or sell_order.get("price"))
            fee_info = sell_order.get("fee") or {}
            fee_usd = float(fee_info.get("cost") or 0.0)
            if fee_usd > 0.0:
                realized_pnl = (exit_price - float(target["entry_price"])) * qty - fee_usd
            pnl_estimated = False
        else:
            exit_price = float(target.get("current_price") or target["entry_price"])
            pnl_estimated = True

        # S5: Record order row for exit
        now_ms = int(time.time() * 1000)
        exit_cid = str(sell_order.get("clientOrderId") or f"CLOSE_{position_id}_{now_ms}")
        self.db.upsert_order({
            "client_order_id": exit_cid,
            "exchange_order_id": str(sell_order.get("id")) if sell_order.get("id") else None,
            "symbol": symbol,
            "side": "sell",
            "order_type": str(sell_order.get("type") or "market").lower(),
            "status": "closed",
            "price": exit_price,
            "stop_price": None,
            "amount": qty,
            "filled": float(sell_order.get("filled") or qty),
            "cost_usd": exit_price * qty,
            "causal_timestamp_ms": now_ms,
            "created_at_ms": now_ms,
            "updated_at_ms": now_ms,
        })

        closed_pos = self.db.close_position(
            position_id=position_id,
            exit_price=exit_price,
            close_reason=reason,
            pnl_estimated=pnl_estimated,
            realized_pnl=realized_pnl,
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
        if (
            "STOP_LOSS" in reason.upper()
            and self.pre_trade_analyzer is not None
            and getattr(self.pre_trade_analyzer, "mistake_learner", None) is not None
            and closed_pos
        ):
            asyncio.create_task(
                self.pre_trade_analyzer.mistake_learner.analyze_and_record_stop_loss(
                    closed_pos=closed_pos,
                    exit_reason=reason,
                )
            )
        return {
            "closed": True,
            "position": closed_pos,
            "sell_order": sell_order,
        }
