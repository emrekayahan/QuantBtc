"""Unit tests for Phase 4: Idempotent clientOrderId recovery and Mandatory Server-Side SL / Emergency Market-Sell."""

from __future__ import annotations

from typing import Any, Dict, List, Optional
import ccxt.async_support as ccxt
import pytest

from config.settings import Settings
from database.db_manager import DatabaseManager
from engine.exchange import BinanceExchangeWrapper
from engine.execution import ExecutionEngine
from engine.risk_engine import RiskEngine
from tests.test_kelly_and_risk import make_valid_decision


class StubCCXTClient:
    """In-memory deterministic test double for Binance Spot order lifecycle verification."""

    def __init__(self, fail_sl: bool = False, timeout_first_entry: bool = False) -> None:
        self.fail_sl = fail_sl
        self.timeout_first_entry = timeout_first_entry
        self.entry_attempts = 0
        self.sl_attempts = 0
        self.market_sell_calls = 0
        self.orders_by_client_id: Dict[str, Dict[str, Any]] = {}
        self.markets = {
            "BTC/USDT": {
                "limits": {"cost": {"min": 10.0}},
                "precision": {"amount": 6, "price": 2},
            }
        }

    async def load_markets(self, reload: bool = False) -> Dict[str, Any]:
        return self.markets

    def amount_to_precision(self, symbol: str, amount: float) -> str:
        return f"{amount:.6f}"

    def price_to_precision(self, symbol: str, price: float) -> str:
        return f"{price:.2f}"

    async def fetch_tickers(self, symbols: Optional[List[str]] = None) -> Dict[str, Any]:
        return {
            "BTC/USDT": {"last": 85000.0, "close": 85000.0},
            "ETH/USDT": {"last": 3000.0, "close": 3000.0},
            "SOL/USDT": {"last": 150.0, "close": 150.0},
        }

    async def fetch_balance(self) -> Dict[str, Any]:
        return {"free": {"USDT": 500.0, "BTC": 0.000294}, "total": {"USDT": 500.0, "BTC": 0.000294}}

    async def fetch_ohlcv(
        self, symbol: str, timeframe: str = "15m", since: Optional[int] = None, limit: int = 250
    ) -> List[List[Any]]:
        ts = since or 1700000000000
        candles = []
        for i in range(limit):
            candles.append([ts + i * 900000, 80000.0, 82500.0, 78500.0, 81000.0, 10.0])
        return candles

    async def fetch_open_orders(
        self, symbol: Optional[str] = None, params: Optional[Dict[str, Any]] = None
    ) -> List[Dict[str, Any]]:
        return list(self.orders_by_client_id.values())

    async def fetch_positions(self, symbols: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        return []

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
            order = {
                "id": "ENTRY_1001",
                "clientOrderId": cid,
                "symbol": symbol,
                "type": order_type,
                "side": side,
                "status": "closed",
                "price": price,
                "average": price,
                "amount": amount,
                "filled": amount,
                "cost": amount * float(price or 0.0),
            }
            self.orders_by_client_id[cid] = order
            if self.timeout_first_entry and self.entry_attempts == 1:
                raise ccxt.RequestTimeout("Simulated timeout after order accepted by Binance")
            return order

        if order_type == "STOP_LOSS_LIMIT":
            self.sl_attempts += 1
            if self.fail_sl:
                raise ccxt.ExchangeError("Simulated STOP_LOSS_LIMIT rejection")
            sl_order = {
                "id": "SL_2002",
                "clientOrderId": cid,
                "symbol": symbol,
                "type": order_type,
                "side": side,
                "status": "open",
                "price": price,
                "stopPrice": params.get("stopPrice"),
                "amount": amount,
                "filled": 0.0,
            }
            self.orders_by_client_id[cid] = sl_order
            return sl_order

        if order_type == "market" and side == "sell":
            self.market_sell_calls += 1
            return {
                "id": "MKT_SELL_3003",
                "symbol": symbol,
                "type": "market",
                "side": "sell",
                "status": "closed",
                "average": 84950.0,
                "amount": amount,
                "filled": amount,
            }

        raise ValueError(f"Unexpected order call: {order_type} {side}")


@pytest.mark.asyncio
async def test_idempotent_timeout_recovery_and_server_side_sl_placement(tmp_path) -> None:
    """
    Verify that when create_order raises RequestTimeout after Binance accepted the order,
    ExecutionEngine queries fetch_order_by_client_order_id(), avoids duplicate entry orders,
    and immediately places the mandatory STOP_LOSS_LIMIT order.
    """
    live_cfg = Settings(
        TRADING_MODE="live",
        BINANCE_API_KEY="dummy_key",
        BINANCE_API_SECRET="dummy_secret",
        DB_PATH=str(tmp_path / "idem_test.db"),
    )
    db = DatabaseManager(db_path=live_cfg.DB_PATH)
    try:
        stub = StubCCXTClient(fail_sl=False, timeout_first_entry=True)
        exchange = BinanceExchangeWrapper(db=db, custom_settings=live_cfg, ccxt_client=stub)
        risk = RiskEngine(db=db, custom_settings=live_cfg, exchange=exchange)
        exec_engine = ExecutionEngine(
            exchange=exchange, db=db, risk_engine=risk, custom_settings=live_cfg
        )

        decision = make_valid_decision(symbol="BTC/USDT")
        result = await exec_engine.execute_approved_entry(decision=decision, free_usdt=500.0)

        assert result["executed"] is True
        assert result["emergency_flattened"] is False
        # Only 1 entry create_order call was made because timeout recovery found the existing order!
        assert stub.entry_attempts == 1
        assert stub.sl_attempts == 1
        assert result["entry_order"]["clientOrderId"] == f"QB_BTCUSDT_{decision.causal_timestamp_ms}"
        assert result["sl_order"]["clientOrderId"] == f"QBSL_BTCUSDT_{decision.causal_timestamp_ms}"
        assert len(db.get_open_positions()) == 1
    finally:
        db.close()


@pytest.mark.asyncio
async def test_sl_failure_after_3_retries_triggers_immediate_market_sell_and_4h_pause(tmp_path) -> None:
    """
    Verify Zero Unprotected Exposure rule: if STOP_LOSS_LIMIT fails 3 times,
    immediately execute create_market_sell_order() and pause symbol for 4 hours.
    """
    live_cfg = Settings(
        TRADING_MODE="live",
        BINANCE_API_KEY="dummy_key",
        BINANCE_API_SECRET="dummy_secret",
        DB_PATH=str(tmp_path / "sl_fail_test.db"),
    )
    db = DatabaseManager(db_path=live_cfg.DB_PATH)
    try:
        stub = StubCCXTClient(fail_sl=True, timeout_first_entry=False)
        exchange = BinanceExchangeWrapper(db=db, custom_settings=live_cfg, ccxt_client=stub)
        risk = RiskEngine(db=db, custom_settings=live_cfg, exchange=exchange)
        exec_engine = ExecutionEngine(
            exchange=exchange, db=db, risk_engine=risk, custom_settings=live_cfg
        )

        decision = make_valid_decision(symbol="BTC/USDT")
        result = await exec_engine.execute_approved_entry(decision=decision, free_usdt=500.0)

        assert result["executed"] is False
        assert result["emergency_flattened"] is True
        assert stub.sl_attempts == 3
        assert stub.market_sell_calls == 1
        assert db.is_symbol_paused("BTC/USDT") is True
        assert len(db.get_open_positions()) == 0
    finally:
        db.close()
