"""Unit tests for T1: Zero unprotected exposure, entry TTL, partial fill SL, and startup recovery."""

from __future__ import annotations

from typing import Any, Dict, List, Optional
import ccxt.async_support as ccxt
import pytest

from config.settings import Settings
from database.db_manager import DatabaseManager
from engine.exchange import BinanceExchangeWrapper
from engine.execution import ExecutionEngine
from engine.reconciler import StateReconciler
from engine.risk_engine import RiskEngine
from tests.test_idempotency import StubCCXTClient
from tests.test_kelly_and_risk import make_valid_decision


class PartialFillCCXTClient(StubCCXTClient):
    """CCXT client that returns a partial fill for entry and allows SL placement."""

    def __init__(self, filled_ratio: float = 0.5) -> None:
        super().__init__()
        self.filled_ratio = filled_ratio

    async def create_order(
        self,
        symbol: str,
        order_type: str,
        side: str,
        amount: float,
        price: Optional[float] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        params = params or {}
        cid = params.get("newClientOrderId", f"AUTO_{len(self.orders_by_client_id) + 1}")

        if side == "buy" and order_type == "limit":
            self.entry_attempts += 1
            filled = amount * self.filled_ratio
            order = {
                "id": "ENTRY_PARTIAL",
                "clientOrderId": cid,
                "symbol": symbol,
                "type": order_type,
                "side": side,
                "status": "open",
                "price": price,
                "average": price,
                "amount": amount,
                "filled": filled,
                "cost": filled * float(price or 0.0),
            }
            self.orders_by_client_id[cid] = order
            return order

        return await super().create_order(symbol, order_type, side, amount, price, params)

    async def fetch_order(self, order_id: str, symbol: Optional[str] = None) -> Dict[str, Any]:
        for o in self.orders_by_client_id.values():
            if o.get("id") == order_id:
                return o
        return {"id": order_id, "status": "closed", "filled": 0.0}

    async def cancel_order(self, order_id: str, symbol: Optional[str] = None) -> Dict[str, Any]:
        for o in self.orders_by_client_id.values():
            if o.get("id") == order_id:
                o["status"] = "canceled"
                return o
        return {"id": order_id, "status": "canceled"}


@pytest.mark.asyncio
async def test_partial_fill_protects_filled_amount_and_cancels_remaining(tmp_path) -> None:
    """Verify that when an entry order partially fills, SL is placed immediately for the filled amount."""
    live_cfg = Settings(
        TRADING_MODE="live",
        ENTRY_ORDER_TTL_SEC=0.1,
        ENTRY_ORDER_POLL_SEC=0.05,
        BINANCE_API_KEY="dummy_key",
        BINANCE_API_SECRET="dummy_secret",
        DB_PATH=str(tmp_path / "partial_test.db"),
    )
    db = DatabaseManager(db_path=live_cfg.DB_PATH)
    try:
        stub = PartialFillCCXTClient(filled_ratio=0.5)
        exchange = BinanceExchangeWrapper(db=db, custom_settings=live_cfg, ccxt_client=stub)
        risk = RiskEngine(db=db, custom_settings=live_cfg, exchange=exchange)
        engine = ExecutionEngine(exchange=exchange, db=db, risk_engine=risk, custom_settings=live_cfg)

        decision = make_valid_decision(symbol="BTC/USDT")
        res = await engine.execute_approved_entry(decision=decision, free_usdt=50.0)

        assert res["executed"] is True
        positions = db.get_open_positions()
        assert len(positions) == 1
        pos = positions[0]
        # Position quantity must match the partial filled amount (sl_qty ~ filled_qty * 0.999 after fee deduction)
        assert pos["quantity"] <= 0.00015 * 0.55
        assert pos["quantity"] >= 0.00015 * 0.45
        assert pos["status"] == "OPEN"
        assert pos["sl_exchange_order_id"] is not None
    finally:
        db.close()


@pytest.mark.asyncio
async def test_startup_recovery_flattens_unprotected_position(tmp_path) -> None:
    """Verify that StateReconciler.run_startup_recovery flattens orphan positions without SL on startup."""
    live_cfg = Settings(
        TRADING_MODE="live",
        BINANCE_API_KEY="dummy_key",
        BINANCE_API_SECRET="dummy_secret",
        DB_PATH=str(tmp_path / "startup_test.db"),
    )
    db = DatabaseManager(db_path=live_cfg.DB_PATH)
    try:
        stub = StubCCXTClient()
        exchange = BinanceExchangeWrapper(db=db, custom_settings=live_cfg, ccxt_client=stub)
        risk = RiskEngine(db=db, custom_settings=live_cfg, exchange=exchange)
        engine = ExecutionEngine(exchange=exchange, db=db, risk_engine=risk, custom_settings=live_cfg)
        reconciler = StateReconciler(exchange=exchange, db=db, execution=engine, custom_settings=live_cfg)

        # Manually inject an OPEN_UNPROTECTED position
        db.open_position({
            "position_id": "POS_ORPHAN_1",
            "symbol": "BTC/USDT",
            "status": "OPEN_UNPROTECTED",
            "entry_client_order_id": "QB_ORPHAN",
            "entry_exchange_order_id": "EX_ORPHAN",
            "sl_client_order_id": None,
            "sl_exchange_order_id": None,
            "entry_price": 85000.0,
            "current_price": 85000.0,
            "invalidation_price": 83000.0,
            "take_profit_price": 90000.0,
            "quantity": 0.0002,
            "cost_usd": 17.0,
        })

        assert len(db.get_open_positions(include_unprotected=True)) == 1

        rec_result = await reconciler.run_startup_recovery()
        assert rec_result["flattened"] == 1
        assert stub.market_sell_calls == 1

        # Must no longer be open in DB
        assert len(db.get_open_positions(include_unprotected=True)) == 0
    finally:
        db.close()
