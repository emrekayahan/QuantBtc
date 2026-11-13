"""30-second State Reconciler between DuckDB and Binance Spot."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

from config.settings import Settings, settings
from database.db_manager import DatabaseManager
from engine.exchange import BinanceExchangeWrapper
from engine.execution import ExecutionEngine
from engine.mistake_learner import MistakeLearner

logger = logging.getLogger(__name__)


class StateReconciler:
    """Synchronizes DuckDB open positions, server-side stop-losses, and dual-wallet USDT equity every 30 seconds."""

    def __init__(
        self,
        exchange: BinanceExchangeWrapper,
        db: DatabaseManager,
        execution: Optional[ExecutionEngine] = None,
        custom_settings: Optional[Settings] = None,
        telegram: Optional[Any] = None,
        mistake_learner: Optional[MistakeLearner] = None,
    ) -> None:
        self.exchange = exchange
        self.db = db
        self.execution = execution
        self.cfg = custom_settings or settings
        self.telegram = telegram
        self._startup_time_ms: int = int(time.time() * 1000)
        self.mistake_learner = mistake_learner or MistakeLearner(
            db=db, telegram=telegram
        )

    async def audit_and_reconcile_invariants(self, tickers: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        Two-way exchange-wide audit enforcing invariants I1 - I5 (Section 4, R4):
        I1: Orphan Position (Exchange > DUST_MAX_USD, DB == 0) -> place SL; if fails, market-close.
        I2: Phantom Position (DB == OPEN, Exchange == 0) -> mark CLOSED.
        I3: Inadequate SL (DB == OPEN, Exchange SL missing or cov < 99%) -> rearm SL immediately.
        I4: SL Dead (Exchange SL canceled/expired/rejected while position open) -> rearm SL immediately.
        I5: Partial Entry (Entry order open/partial with filled > 0 unhedged) -> place SL for filled qty.
        """
        if getattr(self.cfg, "is_paper", True):
            self.db.set_state("entries_blocked", "false")
            self.db.set_state("reconcile_ok", "true")
            self.db.set_state("last_reconcile_ts", str(int(time.time() * 1000)))
            return {"violations": [], "resolved": [], "unresolved": [], "entries_blocked": False}

        violations: List[Dict[str, Any]] = []
        resolved: List[Dict[str, Any]] = []
        unresolved: List[Dict[str, Any]] = []

        dust_max = float(getattr(self.cfg, "DUST_MAX_USD", 0.50))
        t_map = dict(tickers or {})
        if not t_map and hasattr(self.exchange, "fetch_tickers"):
            try:
                t_map = await self.exchange.fetch_tickers()
            except Exception as exc:
                logger.warning("Reconciler failed to fetch tickers: %s", exc)

        # 1. Gather exchange state
        spot_bal = {}
        try:
            spot_bal = await self.exchange.fetch_balance()
        except Exception as exc:
            logger.warning("Reconciler failed to fetch spot balance: %s", exc)

        fut_positions: Dict[str, Dict[str, Any]] = {}
        if hasattr(self.exchange, "fetch_active_futures_positions"):
            try:
                fut_positions = await self.exchange.fetch_active_futures_positions()
            except Exception as exc:
                logger.warning("Reconciler failed to fetch active futures positions: %s", exc)

        # 2. Gather DB state
        db_positions = self.db.get_open_positions(include_unprotected=True)
        db_pos_by_sym = {p["symbol"]: p for p in db_positions}

        # Collect candidate symbols to query open orders per-symbol
        candidate_symbols = set(db_pos_by_sym.keys())
        free_totals = spot_bal.get("total", {}) if isinstance(spot_bal, dict) else {}
        for asset, total_amt in free_totals.items():
            if float(total_amt or 0) > 0 and asset not in ("USDT", "BUSD", "USDC", "FDUSD", "BNB"):
                candidate_symbols.add(f"{asset}/USDT")
        candidate_symbols.update(fut_positions.keys())

        open_orders: List[Dict[str, Any]] = []
        if candidate_symbols:
            for s in candidate_symbols:
                try:
                    s_orders = await self.exchange.fetch_open_orders(s)
                    if s_orders:
                        open_orders.extend(s_orders)
                except Exception as exc:
                    logger.debug("Failed to fetch open orders for symbol %s: %s", s, exc)
                if hasattr(self.exchange, "fetch_futures_open_orders") and getattr(self.exchange, "futures_client", None) is not None:
                    try:
                        f_orders = await self.exchange.fetch_futures_open_orders(s, params={"stop": True})
                        if f_orders:
                            open_orders.extend(f_orders)
                        f_std = await self.exchange.fetch_futures_open_orders(s)
                        if f_std:
                            open_orders.extend(f_std)
                    except Exception as exc:
                        logger.debug("Failed to fetch futures open orders for %s: %s", s, exc)
        else:
            try:
                open_orders = await self.exchange.fetch_open_orders()
            except Exception as exc:
                logger.warning("Reconciler failed to fetch open orders: %s", exc)

        # Map open orders by symbol & type
        open_orders_by_sym: Dict[str, List[Dict[str, Any]]] = {}
        for o in open_orders:
            sym = o.get("symbol") or ""
            open_orders_by_sym.setdefault(sym, []).append(o)

        # ------------------------------------------------------------------
        # INVARIANT I1: Orphan Position (Exchange holds assets, DB has no open position)
        # ------------------------------------------------------------------
        # Spot tokens
        for asset, total_amt in free_totals.items():
            amt = float(total_amt or 0.0)
            if amt <= 0.0 or asset in ("USDT", "BUSD", "USDC", "FDUSD", "BNB"):
                continue
            sym = f"{asset}/USDT"
            px = float(t_map.get(sym, {}).get("last") or t_map.get(sym, {}).get("close") or 0.0)
            val_usd = amt * px
            if sym not in db_pos_by_sym:
                if val_usd <= dust_max:
                    # Dust position: ignore and do not block entries; log INFO only on first seen / value increase
                    v = {"invariant": "I1", "type": "orphan_spot_dust", "symbol": sym, "amount": amt, "val_usd": val_usd}
                    should_log = self.db.upsert_dust_ignored(sym, val_usd)
                    if should_log:
                        self.db.add_reconcile_event("I1_ORPHAN_DUST_IGNORED", sym, v, resolved=True)
                        logger.info("Orphan spot for %s is dust ($%.2f <= $%.2f), ignoring.", sym, val_usd, dust_max)
                    resolved.append(v)
                    continue

                v = {"invariant": "I1", "type": "orphan_spot", "symbol": sym, "amount": amt, "val_usd": val_usd}
                violations.append(v)

                # Check if an active protective SL order already exists on exchange
                sym_orders = open_orders_by_sym.get(sym, [])
                existing_sl = None
                for o in sym_orders:
                    o_type = str(o.get("type", "")).upper()
                    o_side = str(o.get("side", "")).lower()
                    o_amt = float(o.get("amount") or 0.0)
                    if o_side == "sell" and "STOP" in o_type and o_amt >= amt * 0.99:
                        existing_sl = o
                        break

                if existing_sl:
                    # ADOPT POSITION: Existing SL covers position, no new orders needed
                    sl_price = float(existing_sl.get("stopPrice") or existing_sl.get("price") or (px * 0.98))
                    pos_id = f"ADOPTED_{sym.replace('/', '')}_{int(time.time() * 1000)}"
                    self.db.open_position({
                        "position_id": pos_id,
                        "symbol": sym,
                        "entry_client_order_id": f"QB_ADOPTED_{sym.replace('/', '')}",
                        "entry_price": px,
                        "current_price": px,
                        "invalidation_price": sl_price,
                        "take_profit_price": px * 1.03,
                        "quantity": amt,
                        "cost_usd": val_usd,
                        "market_type": "spot",
                        "sl_exchange_order_id": str(existing_sl.get("id")),
                        "sl_client_order_id": str(existing_sl.get("clientOrderId") or f"QBSL_{existing_sl.get('id')}"),
                        "direction_confidence": 0.75,
                        "is_paper": False,
                        "adopted": True,
                    })
                    self.db.add_reconcile_event("I1_ORPHAN_ADOPTED", sym, {"symbol": sym, "amount": amt, "sl_order_id": str(existing_sl.get("id"))}, resolved=True)
                    resolved.append(v)
                    logger.info("Adopted orphan spot position for %s (amount: %s) with existing SL %s", sym, amt, existing_sl.get("id"))
                elif getattr(self.cfg, "RECONCILE_ORPHAN_CLOSE_MARKET", False):
                    # Configured for explicit immediate market close
                    remediated = False
                    if self.execution is not None:
                        try:
                            c_res = await self.exchange.create_order(
                                symbol=sym,
                                order_type="market",
                                side="sell",
                                amount=amt,
                            )
                            if c_res and c_res.get("id"):
                                remediated = True
                                self.db.add_reconcile_event("I1_ORPHAN_SPOT_CLOSED", sym, c_res, resolved=True)
                        except Exception as err:
                            logger.critical("Failed to close orphan spot %s: %s", sym, err)
                    if remediated:
                        resolved.append(v)
                    else:
                        self.db.add_reconcile_event("I1_ORPHAN_SPOT", sym, v, resolved=False)
                        unresolved.append(v)
                else:
                    # Attempt to place SL first before any close (retry up to 3 times)
                    rearmed_sl = None
                    if self.execution is not None and px > 0.0:
                        for attempt in range(1, 4):
                            try:
                                sl_price = px * 0.98
                                rearm_id = f"QBSL_ORPHAN_{int(time.time() * 1000)}"
                                sl_res = await self.exchange.create_order(
                                    symbol=sym,
                                    order_type="STOP_LOSS_LIMIT",
                                    side="sell",
                                    amount=amt,
                                    price=sl_price * 0.995,
                                    params={"stopPrice": sl_price, "clientOrderId": rearm_id, "newClientOrderId": rearm_id},
                                )
                                if sl_res and sl_res.get("id"):
                                    rearmed_sl = sl_res
                                    break
                            except Exception as sl_err:
                                logger.warning("Attempt %d/3: Could not place protective SL for orphan spot %s: %s", attempt, sym, sl_err)
                                if attempt < 3:
                                    await asyncio.sleep(0.5)

                    if rearmed_sl:
                        pos_id = f"ADOPTED_{sym.replace('/', '')}_{int(time.time() * 1000)}"
                        self.db.open_position({
                            "position_id": pos_id,
                            "symbol": sym,
                            "entry_client_order_id": f"QB_ADOPTED_{sym.replace('/', '')}",
                            "entry_price": px,
                            "current_price": px,
                            "invalidation_price": px * 0.98,
                            "take_profit_price": px * 1.03,
                            "quantity": amt,
                            "cost_usd": val_usd,
                            "market_type": "spot",
                            "sl_exchange_order_id": str(rearmed_sl.get("id")),
                            "sl_client_order_id": str(rearmed_sl.get("clientOrderId") or rearm_id),
                            "direction_confidence": 0.75,
                            "is_paper": False,
                            "adopted": True,
                        })
                        self.db.add_reconcile_event("I1_ORPHAN_PROTECTED_AND_ADOPTED", sym, v, resolved=True)
                        resolved.append(v)
                    else:
                        # 3 attempts to place SL failed: market close orphan if funds are not locked
                        free_amt = float(spot_bal.get("free", {}).get(asset, 0.0) or 0.0)
                        if free_amt < amt and sym_orders:
                            self.db.add_reconcile_event("I1_ORPHAN_LOCKED_IN_ORDER", sym, v, resolved=True)
                            resolved.append(v)
                        else:
                            remediated = False
                            if self.execution is not None:
                                try:
                                    c_res = await self.exchange.create_order(
                                        symbol=sym,
                                        order_type="market",
                                        side="sell",
                                        amount=amt,
                                    )
                                    if c_res and c_res.get("id"):
                                        remediated = True
                                        self.db.add_reconcile_event("I1_ORPHAN_SPOT_CLOSED", sym, c_res, resolved=True)
                                except Exception as err:
                                    logger.critical("Failed to close orphan spot %s after 3 SL attempts: %s", sym, err)
                            if remediated:
                                resolved.append(v)
                            else:
                                self.db.add_reconcile_event("I1_ORPHAN_SPOT", sym, v, resolved=False)
                                unresolved.append(v)

        # Futures positions
        for sym, f_pos in fut_positions.items():
            contracts = abs(float(f_pos.get("contracts") or 0.0))
            if contracts <= 0.0 or sym in db_pos_by_sym:
                continue
            v = {"invariant": "I1", "type": "orphan_futures", "symbol": sym, "contracts": contracts}
            violations.append(v)

            # Check if active algo / stop order exists for this futures position
            existing_f_sl = None
            sym_orders = open_orders_by_sym.get(sym, [])
            for o in sym_orders:
                o_type = str(o.get("type", "")).upper()
                if o.get("reduceOnly") or o.get("closePosition") or "STOP" in o_type:
                    existing_f_sl = o
                    break

            if existing_f_sl:
                # ADOPT FUTURES POSITION
                entry_px = float(f_pos.get("entryPrice") or t_map.get(sym, {}).get("last") or 0.0)
                sl_px = float(existing_f_sl.get("stopPrice") or existing_f_sl.get("price") or (entry_px * 0.98))
                pos_id = f"ADOPTED_FUT_{sym.replace('/', '')}_{int(time.time() * 1000)}"
                self.db.open_position({
                    "position_id": pos_id,
                    "symbol": sym,
                    "entry_client_order_id": f"QB_ADOPTED_FUT_{sym.replace('/', '')}",
                    "entry_price": entry_px,
                    "current_price": entry_px,
                    "invalidation_price": sl_px,
                    "take_profit_price": entry_px * 1.03,
                    "quantity": contracts,
                    "cost_usd": float(f_pos.get("notional") or (contracts * entry_px)),
                    "market_type": "futures",
                    "leverage": int(f_pos.get("leverage") or 1),
                    "margin_usd": float(f_pos.get("initialMargin") or 0.0),
                    "notional_usd": float(f_pos.get("notional") or (contracts * entry_px)),
                    "sl_exchange_order_id": str(existing_f_sl.get("id")),
                    "sl_client_order_id": str(existing_f_sl.get("clientOrderId") or f"QBSL_{existing_f_sl.get('id')}"),
                    "direction_confidence": 0.75,
                    "is_paper": False,
                    "adopted": True,
                })
                self.db.add_reconcile_event("I1_ORPHAN_FUTURES_ADOPTED", sym, v, resolved=True)
                resolved.append(v)
            else:
                # Try placing SL first before closing (retry up to 3 times)
                f_sl_placed = None
                if self.execution is not None:
                    entry_px = float(f_pos.get("entryPrice") or t_map.get(sym, {}).get("last") or 0.0)
                    f_side = "sell" if str(f_pos.get("side", "")).lower() in ("long", "buy") else "buy"
                    stop_px = entry_px * 0.98 if f_side == "sell" else entry_px * 1.02
                    rearm_id = f"QBSL_FUT_{int(time.time() * 1000)}"
                    for attempt in range(1, 4):
                        try:
                            f_sl_res = await self.exchange.create_futures_order(
                                symbol=sym,
                                order_type="STOP_MARKET",
                                side=f_side,
                                amount=contracts,
                                price=None,
                                params={"stopPrice": stop_px, "reduceOnly": True, "clientOrderId": rearm_id},
                            )
                            if f_sl_res and f_sl_res.get("id"):
                                f_sl_placed = f_sl_res
                                break
                        except Exception as f_err:
                            logger.warning("Attempt %d/3: Could not place SL for orphan futures %s: %s", attempt, sym, f_err)
                            if attempt < 3:
                                await asyncio.sleep(0.5)

                if f_sl_placed:
                    pos_id = f"ADOPTED_FUT_{sym.replace('/', '')}_{int(time.time() * 1000)}"
                    self.db.open_position({
                        "position_id": pos_id,
                        "symbol": sym,
                        "entry_client_order_id": f"QB_ADOPTED_FUT_{sym.replace('/', '')}",
                        "entry_price": entry_px,
                        "current_price": entry_px,
                        "invalidation_price": stop_px,
                        "take_profit_price": entry_px * 1.03,
                        "quantity": contracts,
                        "cost_usd": float(f_pos.get("notional") or (contracts * entry_px)),
                        "market_type": "futures",
                        "leverage": int(f_pos.get("leverage") or 1),
                        "margin_usd": float(f_pos.get("initialMargin") or 0.0),
                        "notional_usd": float(f_pos.get("notional") or (contracts * entry_px)),
                        "sl_exchange_order_id": str(f_sl_placed.get("id")),
                        "sl_client_order_id": str(f_sl_placed.get("clientOrderId") or rearm_id),
                        "direction_confidence": 0.75,
                        "is_paper": False,
                        "adopted": True,
                    })
                    self.db.add_reconcile_event("I1_ORPHAN_FUTURES_PROTECTED_AND_ADOPTED", sym, v, resolved=True)
                    resolved.append(v)
                else:
                    # SL placement failed; only now market close with reduceOnly
                    remediated = False
                    if self.execution is not None:
                        try:
                            c_res = await self.exchange.create_futures_order(
                                symbol=sym,
                                order_type="market",
                                side="sell" if str(f_pos.get("side", "")).lower() in ("long", "buy") else "buy",
                                amount=contracts,
                                price=None,
                                params={"reduceOnly": True},
                            )
                            if c_res and c_res.get("id"):
                                remediated = True
                                self.db.add_reconcile_event("I1_ORPHAN_FUTURES_CLOSED", sym, c_res, resolved=True)
                        except Exception as err:
                            logger.critical("Failed to remediate I1 orphan futures for %s: %s", sym, err)

                    if remediated:
                        resolved.append(v)
                    else:
                        self.db.add_reconcile_event("I1_ORPHAN_FUTURES", sym, v, resolved=False)
                        unresolved.append(v)

        # ------------------------------------------------------------------
        # INVARIANTS I2, I3, I4: Inspect each DB open position against exchange
        # ------------------------------------------------------------------
        for pos in db_positions:
            sym = pos["symbol"]
            pos_id = pos["position_id"]
            db_qty = float(pos.get("quantity") or 0.0)
            m_type = str(pos.get("market_type", "spot")).lower()
            sl_exchange_id = str(pos.get("sl_exchange_order_id") or "")
            inval_price = float(pos.get("invalidation_price") or 0.0)

            # Determine actual exchange holding
            exch_qty = 0.0
            if m_type == "futures":
                f_p = fut_positions.get(sym)
                if f_p is not None:
                    exch_qty = abs(float(f_p.get("contracts") or 0.0))
            else:
                base_asset = sym.split("/")[0]
                exch_qty = float(free_totals.get(base_asset, 0.0) or 0.0)

            px = float(t_map.get(sym, {}).get("last") or t_map.get(sym, {}).get("close") or pos.get("entry_price") or 0.0)
            val_usd = exch_qty * px

            # INVARIANT I2: Phantom Position (DB has OPEN, but exchange balance is 0 or dust)
            if exch_qty <= 0.0 or (m_type != "futures" and val_usd <= dust_max):
                v = {"invariant": "I2", "symbol": sym, "position_id": pos_id, "db_qty": db_qty, "exch_qty": exch_qty}
                violations.append(v)
                self.db.add_reconcile_event("I2_PHANTOM_POSITION", sym, v, resolved=False)

                # K0-6: Lookup real fill price before falling back to ticker estimate
                real_exit_price = px
                close_reason = "PHANTOM_POSITION_RESOLVED_CLOSED"
                pnl_estimated = True
                sl_oid = pos.get("sl_exchange_order_id")
                if sl_oid:
                    try:
                        sl_info = (
                            await self.exchange.fetch_futures_order(str(sl_oid), sym)
                            if m_type == "futures" and hasattr(self.exchange, "fetch_futures_order")
                            else await self.exchange.fetch_order(str(sl_oid), sym)
                        )
                        if sl_info and str(sl_info.get("status") or "").lower() in ("closed", "filled"):
                            fill_p = float(sl_info.get("average") or sl_info.get("price") or sl_info.get("stopPrice") or 0.0)
                            if fill_p > 0:
                                real_exit_price = fill_p
                                close_reason = "SERVER_SIDE_STOP_LOSS_FILLED"
                                pnl_estimated = False
                    except Exception:
                        pass

                if pnl_estimated and hasattr(self.exchange, "fetch_my_trades"):
                    try:
                        opened_ms = int(pos.get("opened_at_ms") or 0)
                        trades = await self.exchange.fetch_my_trades(sym, limit=10)
                        matching = [
                            t for t in (trades or [])
                            if int(t.get("timestamp") or 0) >= opened_ms and str(t.get("side") or "").lower() == "sell"
                        ]
                        if matching:
                            last_fill = matching[-1]
                            fill_p = float(last_fill.get("price") or 0.0)
                            if fill_p > 0:
                                real_exit_price = fill_p
                                pnl_estimated = False
                    except Exception:
                        pass

                # Auto-remediate I2: mark closed in DB
                self.db.close_position(
                    position_id=pos_id,
                    exit_price=real_exit_price,
                    close_reason=close_reason,
                    pnl_estimated=pnl_estimated,
                )
                self.db.add_reconcile_event("I2_PHANTOM_RESOLVED", sym, v, resolved=True)
                resolved.append(v)
                continue

            status = pos.get("status", "OPEN")

            # INVARIANTS I3 & I4: Check SL coverage & status
            sym_open_orders = open_orders_by_sym.get(sym, [])
            active_sl_order = None
            if sl_exchange_id:
                active_sl_order = next((o for o in sym_open_orders if str(o.get("id")) == sl_exchange_id), None)
                if active_sl_order is None and hasattr(self.exchange, "fetch_futures_order") and m_type == "futures":
                    try:
                        fut_o = await self.exchange.fetch_futures_order(sl_exchange_id, sym)
                        if fut_o and str(fut_o.get("status") or "").lower() in ("open", "new"):
                            active_sl_order = fut_o
                    except Exception:
                        pass

            sl_coverage_ok = False
            if active_sl_order and status != "OPEN_UNPROTECTED":
                sl_amt = float(active_sl_order.get("amount") or 0.0)
                target_qty = min(exch_qty, db_qty) if db_qty > 0 else exch_qty
                if sl_amt >= target_qty * 0.99:
                    sl_coverage_ok = True

            # If no active SL or inadequate coverage (I3 / I4) or OPEN_UNPROTECTED
            if not sl_coverage_ok:
                inv_name = "I4" if sl_exchange_id else "I3"
                v = {
                    "invariant": inv_name,
                    "symbol": sym,
                    "position_id": pos_id,
                    "exch_qty": exch_qty,
                    "sl_order_id": sl_exchange_id,
                }
                violations.append(v)
                self.db.add_reconcile_event(f"{inv_name}_SL_VIOLATION", sym, v, resolved=False)

                # Remediation: Attempt to re-arm SL immediately only if position is regular OPEN with valid inval_price
                rearmed = False
                sl_buf_mult = 1.0 - (float(getattr(self.cfg, "SL_LIMIT_BUFFER_PCT", 0.20)) / 100.0)
                if self.execution is not None and inval_price > 0.0 and status != "OPEN_UNPROTECTED":
                    try:
                        rearm_id = f"QBSL_REARM_{int(time.time() * 1000)}"
                        if m_type == "futures":
                            fut_stop = self.exchange.futures_price_to_precision(sym, inval_price)
                            fut_limit = self.exchange.futures_price_to_precision(sym, inval_price * sl_buf_mult)
                            fut_qty = self.exchange.futures_amount_to_precision(sym, exch_qty)
                            new_sl = await self.exchange.create_futures_order(
                                symbol=sym,
                                order_type="STOP",
                                side="sell",
                                amount=fut_qty,
                                price=fut_limit,
                                params={"stopPrice": fut_stop, "reduceOnly": True, "newClientOrderId": rearm_id},
                            )
                        else:
                            spot_stop = self.exchange.price_to_precision(sym, inval_price)
                            spot_limit = self.exchange.price_to_precision(sym, inval_price * sl_buf_mult)
                            spot_qty = self.exchange.amount_to_precision(sym, exch_qty)
                            new_sl = await self.exchange.create_order(
                                symbol=sym,
                                order_type="STOP_LOSS_LIMIT",
                                side="sell",
                                amount=spot_qty,
                                price=spot_limit,
                                params={"stopPrice": spot_stop, "timeInForce": "GTC", "newClientOrderId": rearm_id},
                            )

                        if new_sl and new_sl.get("id"):
                            new_sl_id = str(new_sl["id"])
                            self.db.open_position({
                                **pos,
                                "status": "OPEN",
                                "sl_exchange_order_id": new_sl_id,
                                "sl_client_order_id": rearm_id,
                            })
                            self.db.add_reconcile_event(f"{inv_name}_SL_REARMED", sym, {"new_sl_id": new_sl_id}, resolved=True)
                            rearmed = True
                    except Exception as rearm_exc:
                        logger.critical("Failed to rearm SL for %s: %s", sym, rearm_exc)

                if rearmed:
                    resolved.append(v)
                else:
                    unresolved.append(v)

        # ------------------------------------------------------------------
        # INVARIANT I5: Partial Entry (Open buy orders that have partial fills but no SL)
        # ------------------------------------------------------------------
        for o in open_orders:
            side = str(o.get("side") or "").lower()
            filled = float(o.get("filled") or 0.0)
            sym = str(o.get("symbol") or "")
            if side == "buy" and filled > 0.0:
                base_asset = sym.split("/")[0]
                curr_asset_val = float(free_totals.get(base_asset, 0.0) or 0.0) * float(t_map.get(sym, {}).get("last") or 0.0)
                if curr_asset_val > dust_max and sym not in db_pos_by_sym:
                    v = {"invariant": "I5", "symbol": sym, "order_id": str(o.get("id")), "filled": filled}
                    violations.append(v)
                    self.db.add_reconcile_event("I5_PARTIAL_ENTRY", sym, v, resolved=False)
                    # Remediation: cancel lingering entry buy and place market sell/SL
                    remediated = False
                    try:
                        await self.exchange.cancel_order(str(o.get("id")), sym)
                        if self.execution is not None:
                            sell_res = await self.exchange.create_market_sell_order(sym, filled)
                            if sell_res and sell_res.get("id"):
                                remediated = True
                                self.db.add_reconcile_event("I5_PARTIAL_ENTRY_RESOLVED", sym, sell_res, resolved=True)
                    except Exception as p_exc:
                        logger.critical("Failed to remediate I5 partial entry on %s: %s", sym, p_exc)

                    if remediated:
                        resolved.append(v)
                    else:
                        unresolved.append(v)

        # Determine blocking state
        entries_blocked = len(unresolved) > 0
        self.db.set_state("entries_blocked", "true" if entries_blocked else "false")
        self.db.set_state("reconcile_ok", "false" if entries_blocked else "true")
        self.db.set_state("last_reconcile_ts", str(int(time.time() * 1000)))

        return {
            "violations": violations,
            "resolved": resolved,
            "unresolved": unresolved,
            "entries_blocked": entries_blocked,
        }

    async def run_startup_recovery(self) -> Dict[str, Any]:
        """
        Audit exchange vs DuckDB open positions on startup.
        Runs full invariant audit: attempts SL placement first; only market-flattens if SL placement fails.
        """
        if getattr(self.cfg, "is_paper", True):
            logger.info("Startup recovery: Running in PAPER mode, exchange recovery skipped.")
            return {"recovered": 0, "flattened": 0, "status": "paper_ok"}

        recovered = 0
        flattened = 0

        tickers: Dict[str, Any] = {}
        try:
            tickers = await self.exchange.fetch_tickers()
        except Exception as exc:
            logger.warning("Startup recovery failed to fetch tickers: %s", exc)

        audit_res = await self.audit_and_reconcile_invariants(tickers=tickers)
        recovered = len(audit_res.get("resolved", []))

        # Check any still unprotected positions and flatten them if needed
        unprotected_positions = [
            p for p in self.db.get_open_positions(include_unprotected=True)
            if p.get("status") == "OPEN_UNPROTECTED" or not p.get("sl_exchange_order_id")
        ]

        for pos in unprotected_positions:
            pos_id = pos["position_id"]
            symbol = pos["symbol"]
            logger.warning("Startup recovery: Flattening lingering unprotected position %s for %s", pos_id, symbol)
            if self.execution is not None:
                try:
                    res = await self.execution.close_single_position_market(
                        position_id=pos_id, reason="STARTUP_UNPROTECTED_RECOVERY"
                    )
                    if res.get("closed"):
                        flattened += 1
                except Exception as rec_exc:
                    logger.critical("Startup recovery failed to flatten %s: %s", pos_id, rec_exc)

        # Re-check blocking state after startup recovery
        has_lingering_unprotected = len([
            p for p in self.db.get_open_positions(include_unprotected=True)
            if p.get("status") == "OPEN_UNPROTECTED" or not p.get("sl_exchange_order_id")
        ]) > 0
        if not has_lingering_unprotected and len(audit_res.get("unresolved", [])) == 0:
            self.db.set_state("entries_blocked", "false")
            self.db.set_state("reconcile_ok", "true")

        return {
            "recovered": recovered,
            "flattened": flattened,
            "status": "completed",
            "entries_blocked": self.db.get_state("entries_blocked", "false") == "true",
        }

    async def reconcile_cycle(self) -> Dict[str, Any]:
        """
        1. Fetch live Binance Spot & USD-M Futures balances and tickers.
        2. In Live mode, audit and auto-remediate invariants I1 - I5.
        3. Update current prices, unrealized PnL, total USDT equity, peak equity, and drawdown %.
        4. Check if any open position hit take_profit_price or if its Server-Side SL filled on Binance.
        """
        # In Paper mode, simulate balances and reconcile paper positions using live tickers
        if getattr(self.cfg, "is_paper", True):
            open_positions = self.db.get_open_positions()
            tickers: Dict[str, Any] = {}
            if open_positions:
                try:
                    tickers = await self.exchange.fetch_tickers()
                except Exception as exc:
                    logger.warning("Paper reconciler failed to fetch tickers: %s", exc)

            reconciled_closed = 0
            tp_triggered = 0
            spot_positions_value_usd = 0.0
            futures_equity_usd = 0.0

            for pos in open_positions:
                symbol = pos["symbol"]
                pos_id = pos["position_id"]
                qty = float(pos["quantity"])
                tp_price = float(pos["take_profit_price"])
                invalidation_price = float(pos["invalidation_price"])
                t = tickers.get(symbol, {})
                live_price = float(t.get("last") or t.get("close") or pos.get("current_price") or pos["entry_price"])
                self.db.update_position_price(pos_id, live_price)

                if live_price <= invalidation_price and self.execution is not None:
                    await self.execution.close_single_position_market(position_id=pos_id, reason="STOP_LOSS_HIT")
                    reconciled_closed += 1
                    continue
                if live_price >= tp_price and self.execution is not None:
                    await self.execution.close_single_position_market(position_id=pos_id, reason="TAKE_PROFIT_HIT")
                    tp_triggered += 1
                    continue

                m_type = str(pos.get("market_type", "spot")).lower()
                if m_type == "futures":
                    margin = float(pos.get("margin_usd", 0.0))
                    entry_p = float(pos.get("entry_price", live_price))
                    u_pnl = (live_price - entry_p) * qty
                    futures_equity_usd += (margin + u_pnl)
                else:
                    spot_positions_value_usd += qty * live_price

            paper_wallet = float(self.db.get_state("paper_wallet_balance_usdt", "30.0") or 30.0)
            daily_pnl = self.db.get_daily_realized_pnl()
            total_equity_usd = paper_wallet + spot_positions_value_usd + futures_equity_usd
            free_usdt = paper_wallet

            peak_equity = float(self.db.get_state("peak_equity_usd", "0.0") or 0.0)
            if total_equity_usd > peak_equity:
                peak_equity = total_equity_usd
                self.db.set_state("peak_equity_usd", f"{peak_equity:.2f}")

            drawdown_pct = 0.0
            if peak_equity > 0.0:
                drawdown_pct = max(0.0, (peak_equity - total_equity_usd) / peak_equity * 100.0)

            self.db.set_state("free_usdt", f"{free_usdt:.2f}")
            self.db.set_state("spot_free_usdt", f"{free_usdt:.2f}")
            self.db.set_state("futures_free_usdt", "0.00")
            self.db.set_state("futures_total_usdt", f"{futures_equity_usd:.2f}")
            self.db.set_state("active_trading_mode", "PAPER")
            self.db.set_state("total_equity_usd", f"{total_equity_usd:.2f}")
            self.db.set_state("current_drawdown_pct", f"{drawdown_pct:.2f}")
            self.db.set_state("daily_realized_pnl_usd", f"{daily_pnl:.2f}")
            self.db.set_state("reconcile_ok", "true")
            self.db.set_state("entries_blocked", "false")
            self.db.set_state("last_reconcile_ts", str(int(time.time() * 1000)))

            if drawdown_pct >= float(getattr(self.cfg, "MAX_DRAWDOWN_PCT", 15.0)):
                logger.critical("PAPER MAX DRAWDOWN BREACH: %.2f%% >= %.2f%%", drawdown_pct, self.cfg.MAX_DRAWDOWN_PCT)
                if getattr(self, "risk_engine", None) is not None:
                    await self.risk_engine.trigger_emergency_kill_switch(f"PAPER_MAX_DRAWDOWN_BREACH: {drawdown_pct:.2f}%")

            return {
                "free_usdt": round(free_usdt, 2),
                "spot_free_usdt": round(free_usdt, 2),
                "futures_free_usdt": 0.0,
                "active_trading_mode": "PAPER",
                "total_equity_usd": round(total_equity_usd, 2),
                "drawdown_pct": round(drawdown_pct, 2),
                "daily_realized_pnl_usd": round(daily_pnl, 2),
                "reconciled_closed": reconciled_closed,
                "tp_triggered": tp_triggered,
                "is_paper": True,
            }
        if hasattr(self.exchange, "fetch_dual_wallet_balances"):
            dual = await self.exchange.fetch_dual_wallet_balances()
            spot_free_usdt = float(dual.get("spot_free_usdt", 0.0))
            spot_total_usdt = float(dual.get("spot_total_usdt", spot_free_usdt))
            futures_free_usdt = float(dual.get("futures_free_usdt", 0.0))
            futures_total_usdt = float(dual.get("futures_total_usdt", futures_free_usdt))
            active_mode = str(dual.get("active_mode", "SPOT"))
            free_usdt = spot_free_usdt
            total_usdt_cash = spot_total_usdt + futures_total_usdt
        else:
            balance = await self.exchange.fetch_balance()
            free_usdt = float(balance.get("free", {}).get("USDT", 0.0) or 0.0)
            total_usdt_cash = float(balance.get("total", {}).get("USDT", free_usdt) or free_usdt)
            spot_free_usdt = free_usdt
            futures_free_usdt = 0.0
            active_mode = "SPOT"

        tickers: Dict[str, Any] = {}
        fut_positions: Dict[str, Dict[str, Any]] = {}
        try:
            tickers = await self.exchange.fetch_tickers()
        except Exception as exc:
            logger.warning("Reconciler failed to fetch tickers: %s", exc)
        if hasattr(self.exchange, "fetch_active_futures_positions"):
            fut_positions = await self.exchange.fetch_active_futures_positions()

        # In live mode, run the invariant audit and remediation
        audit_res = await self.audit_and_reconcile_invariants(tickers=tickers)
        open_positions = self.db.get_open_positions()

        positions_value_usd = 0.0
        reconciled_closed = 0
        tp_triggered = 0

        for pos in open_positions:
            symbol = pos["symbol"]
            pos_id = pos["position_id"]
            qty = float(pos["quantity"])
            sl_order_id = pos.get("sl_exchange_order_id")
            tp_price = float(pos["take_profit_price"])
            invalidation_price = float(pos["invalidation_price"])

            t = tickers.get(symbol, {})
            live_price = float(
                t.get("last") or t.get("close") or pos.get("current_price") or pos["entry_price"]
            )
            self.db.update_position_price(pos_id, live_price)

            # Check if Server-Side SL filled on Binance (Futures or Spot)
            sl_filled = False
            if sl_order_id:
                try:
                    sl_info = None
                    if hasattr(self.exchange, "fetch_futures_order"):
                        sl_info = await self.exchange.fetch_futures_order(str(sl_order_id), symbol)
                    if sl_info is None:
                        sl_info = await self.exchange.fetch_order(str(sl_order_id), symbol)
                    sl_status = str((sl_info or {}).get("status") or "").lower()
                    if sl_status in ("closed", "filled"):
                        fill_p = float(
                            sl_info.get("average")
                            or sl_info.get("price")
                            or sl_info.get("stopPrice")
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
                        if self.mistake_learner is not None and closed_p:
                            asyncio.create_task(
                                self.mistake_learner.analyze_and_record_stop_loss(
                                    closed_pos=closed_p,
                                    exit_reason="SERVER_SIDE_STOP_LOSS_FILLED",
                                )
                            )
                    elif sl_status in ("open", "untriggered", "new"):
                        # S7: SL escalation check - if price breached invalidation_price and SL limit order remained unfilled for > SL_ESCALATION_SEC, escalate to market
                        sl_order_ts = int(sl_info.get("timestamp") or 0)
                        now_ms = int(time.time() * 1000)
                        escalation_ms = float(getattr(self.cfg, "SL_ESCALATION_SEC", 20.0)) * 1000
                        if live_price <= invalidation_price and sl_order_ts > 0 and (now_ms - sl_order_ts) >= escalation_ms:
                            logger.warning(
                                "SL order %s for %s has not filled after %.1fs of price breaching invalidation price (%.4f <= %.4f). Escalating to market!",
                                sl_order_id, symbol, (now_ms - sl_order_ts) / 1000.0, live_price, invalidation_price
                            )
                            if self.execution is not None:
                                try:
                                    await self.execution.close_single_position_market(
                                        position_id=pos_id, reason="SL_LIMIT_TIMEOUT_ESCALATED_TO_MARKET"
                                    )
                                    reconciled_closed += 1
                                    sl_filled = True
                                    continue
                                except Exception as esc_err:
                                    logger.error("Failed to escalate SL to market for %s: %s", pos_id, esc_err)
                except Exception as exc:
                    logger.debug("Could not fetch SL order %s for %s: %s", sl_order_id, symbol, exc)

            if sl_filled:
                continue

            # Check if Stop-Loss price reached or breached (Software SL Backup)
            if live_price <= invalidation_price and self.execution is not None:
                try:
                    await self.execution.close_single_position_market(
                        position_id=pos_id, reason="STOP_LOSS_HIT"
                    )
                    reconciled_closed += 1
                    continue
                except Exception as exc:
                    logger.error("Failed to close position %s at Stop-Loss: %s", pos_id, exc)

            # R16: Time exit check (MAX_HOLD_BARS = 32 bars * 15m = 8 hours)
            max_hold_bars = int(getattr(self.cfg, "MAX_HOLD_BARS", 32))
            max_hold_ms = max_hold_bars * 15 * 60 * 1000
            opened_ms = int(pos.get("opened_at_ms", 0) or 0)
            now_ms = int(time.time() * 1000)
            grace_hours = float(getattr(self.cfg, "LEGACY_POSITION_GRACE_HOURS", 24.0))
            grace_ms = grace_hours * 3600 * 1000
            is_legacy = bool(pos.get("adopted") or (opened_ms > 0 and opened_ms < self._startup_time_ms))
            time_since_startup = now_ms - self._startup_time_ms

            if opened_ms > 0 and (now_ms - opened_ms) >= max_hold_ms and self.execution is not None:
                if is_legacy and time_since_startup < grace_ms:
                    logger.info(
                        "Position %s is within legacy grace window (%.1fh remaining). Postponing time exit.",
                        pos_id, max(0.0, (grace_ms - time_since_startup) / 3600000.0)
                    )
                else:
                    try:
                        await self.execution.close_single_position_market(
                            position_id=pos_id, reason="MAX_HOLD_TIME_EXPIRED"
                        )
                        reconciled_closed += 1
                        continue
                    except Exception as exc:
                        logger.error("Failed to close position %s at MAX_HOLD_TIME_EXPIRED: %s", pos_id, exc)

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

            # Only add token value for Spot positions (Futures equity is already included in futures_total_usdt)
            if symbol not in fut_positions:
                positions_value_usd += qty * live_price

        # Single source of truth: total_equity = spot_equity (free + token values) + futures_wallet_balance (which includes unrealized PnL)
        total_equity_usd = spot_total_usdt + positions_value_usd + futures_total_usdt
        prev_peak = float(self.db.get_state("peak_equity_usd", "0.0") or 0.0)
        if prev_peak > total_equity_usd * 1.35:
            prev_peak = total_equity_usd
        peak_equity = max(prev_peak, total_equity_usd)
        drawdown_pct = (
            ((peak_equity - total_equity_usd) / peak_equity) * 100.0 if peak_equity > 0.0 else 0.0
        )
        daily_pnl = self.db.get_daily_realized_pnl()

        self.db.set_state("free_usdt", f"{free_usdt:.2f}")
        self.db.set_state("spot_free_usdt", f"{spot_free_usdt:.2f}")
        self.db.set_state("futures_free_usdt", f"{futures_free_usdt:.2f}")
        self.db.set_state("futures_total_usdt", f"{futures_total_usdt:.2f}")
        self.db.set_state("active_trading_mode", active_mode)
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
            "spot_free_usdt": round(spot_free_usdt, 2),
            "futures_free_usdt": round(futures_free_usdt, 2),
            "active_trading_mode": active_mode,
            "total_equity_usd": round(total_equity_usd, 2),
            "drawdown_pct": round(max(0.0, drawdown_pct), 2),
            "daily_realized_pnl_usd": round(daily_pnl, 2),
            "reconciled_closed": reconciled_closed,
            "tp_triggered": tp_triggered,
            "reconcile_ok": not audit_res.get("entries_blocked", False),
            "entries_blocked": audit_res.get("entries_blocked", False),
            "violations_count": len(audit_res.get("violations", [])),
        }
