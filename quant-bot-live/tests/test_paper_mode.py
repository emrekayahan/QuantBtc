"""Unit tests for T0: Paper trading simulation mode and guardrails."""

from __future__ import annotations

import pytest

from config.settings import Settings
from database.db_manager import DatabaseManager
from engine.exchange import BinanceExchangeWrapper
from engine.execution import ExecutionEngine
from engine.risk_engine import RiskEngine
from tests.test_idempotency import StubCCXTClient
from tests.test_kelly_and_risk import make_valid_decision


@pytest.mark.asyncio
async def test_paper_mode_does_not_call_exchange(tmp_path) -> None:
    """Verify that in paper mode, ExecutionEngine simulates execution without calling CCXT create_order."""
    paper_settings = Settings(
        TRADING_MODE="paper",
        BINANCE_API_KEY="dummy_key",
        BINANCE_API_SECRET="dummy_secret",
        DB_PATH=str(tmp_path / "paper_test.db"),
    )
    db = DatabaseManager(db_path=paper_settings.DB_PATH)
    try:
        stub = StubCCXTClient()
        exchange = BinanceExchangeWrapper(db=db, custom_settings=paper_settings, ccxt_client=stub)
        risk = RiskEngine(db=db, custom_settings=paper_settings, exchange=exchange)
        engine = ExecutionEngine(
            exchange=exchange,
            db=db,
            risk_engine=risk,
            custom_settings=paper_settings,
        )

        decision = make_valid_decision(symbol="BTC/USDT")
        res = await engine.execute_approved_entry(decision=decision, free_usdt=50.0)

        assert res["executed"] is True
        assert res.get("is_paper") is True
        # Exchange create_order must NEVER be called in paper mode
        assert stub.entry_attempts == 0
        assert stub.sl_attempts == 0

        # DB must have recorded position as is_paper=True
        positions = db.get_open_positions()
        assert len(positions) == 1
        pos = positions[0]
        assert pos["symbol"] == "BTC/USDT"
        assert bool(pos.get("is_paper")) is True

        # Test paper close
        close_res = await engine.close_single_position_market(pos["position_id"], reason="TEST_CLOSE")
        assert close_res["closed"] is True
        assert close_res.get("is_paper") is True
        assert stub.market_sell_calls == 0
        assert len(db.get_open_positions()) == 0
    finally:
        db.close()


@pytest.mark.asyncio
async def test_live_mode_calls_exchange(tmp_path) -> None:
    """Verify that in live mode, ExecutionEngine places real orders via exchange client."""
    live_settings = Settings(
        TRADING_MODE="live",
        BINANCE_API_KEY="dummy_key",
        BINANCE_API_SECRET="dummy_secret",
        DB_PATH=str(tmp_path / "live_test.db"),
    )
    db = DatabaseManager(db_path=live_settings.DB_PATH)
    try:
        stub = StubCCXTClient()
        exchange = BinanceExchangeWrapper(db=db, custom_settings=live_settings, ccxt_client=stub)
        risk = RiskEngine(db=db, custom_settings=live_settings, exchange=exchange)
        engine = ExecutionEngine(
            exchange=exchange,
            db=db,
            risk_engine=risk,
            custom_settings=live_settings,
        )

        decision = make_valid_decision(symbol="BTC/USDT")
        res = await engine.execute_approved_entry(decision=decision, free_usdt=50.0)

        assert res["executed"] is True
        assert res.get("is_paper") is not True
        assert stub.entry_attempts >= 1
        assert stub.sl_attempts >= 1

        positions = db.get_open_positions()
        assert len(positions) == 1
        assert bool(positions[0].get("is_paper")) is False
    finally:
        db.close()
