"""Unit tests for T2: Single source of truth exchange reconciliation and dual-wallet accounting."""

from __future__ import annotations

from typing import Any, Dict, List, Optional
import asyncio
import pytest

from config.settings import Settings
from database.db_manager import DatabaseManager
from engine.exchange import BinanceExchangeWrapper
from engine.reconciler import StateReconciler
from tests.test_idempotency import StubCCXTClient


class DualWalletReconciliationStub(StubCCXTClient):
    """Stub CCXT client with both Spot and Futures balances."""

    async def fetch_balance(self) -> Dict[str, Any]:
        return {"free": {"USDT": 10.0}, "total": {"USDT": 10.0}}

    async def fetch_tickers(self) -> Dict[str, Any]:
        return {"BTC/USDT": {"last": 85000.0, "close": 85000.0}}

    async def fetch_active_futures_positions(self) -> Dict[str, Dict[str, Any]]:
        return {"BTC/USDT": {"symbol": "BTC/USDT", "contracts": 0.001}}


@pytest.mark.asyncio
async def test_reconciliation_single_source_of_truth_equity(tmp_path) -> None:
    """Verify that total equity equals spot equity + futures wallet equity."""
    live_cfg = Settings(
        TRADING_MODE="live",
        BINANCE_API_KEY="dummy_key",
        BINANCE_API_SECRET="dummy_secret",
        DB_PATH=str(tmp_path / "recon_test.db"),
    )
    db = DatabaseManager(db_path=live_cfg.DB_PATH)
    try:
        stub = DualWalletReconciliationStub()
        exchange = BinanceExchangeWrapper(db=db, custom_settings=live_cfg, ccxt_client=stub)
        exchange.fetch_futures_balance = lambda: asyncio.sleep(0, result={"free": 5.0, "total": 20.0})
        reconciler = StateReconciler(exchange=exchange, db=db, custom_settings=live_cfg)

        res = await reconciler.reconcile_cycle()

        assert res["spot_free_usdt"] == 10.0
        assert res["futures_free_usdt"] == 5.0
        # total_equity = spot_total (10.0) + futures_total (20.0) = 30.0
        assert res["total_equity_usd"] == 30.0
        assert db.get_state("total_equity_usd") == "30.00"
    finally:
        db.close()
