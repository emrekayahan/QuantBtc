"""Round 3 Regression Tests: S1 (resolver since parameter), S3 (log filter %d formatting), and S2 (orphan adoption with existing SL)."""

import logging
import time
import pytest
from config.log_filter import SensitiveDataFilter
from config.settings import Settings
from database.db_manager import DatabaseManager
from engine.exchange import BinanceExchangeWrapper
from engine.outcome_resolver import OutcomeResolver
from engine.reconciler import StateReconciler
from tests.test_idempotency import StubCCXTClient


# --- S3 Tests ---
def test_filter_keeps_int_args(caplog) -> None:
    """S3 Regression: Verify SensitiveDataFilter does not convert int args to str so %d formatting works."""
    caplog.set_level(logging.INFO)
    logger = logging.getLogger("test_httpx_logger")
    logger.handlers.clear()
    logger.addFilter(SensitiveDataFilter())

    # This is the exact formatting pattern httpx uses:
    logger.info('HTTP Request: %s %s "%s %d %s"', "GET", "https://api.binance.com", "HTTP/1.1", 200, "OK")
    assert "Logging error" not in caplog.text
    assert "200 OK" in caplog.text


def test_filter_masks_token_in_str_args(caplog) -> None:
    """S3 Regression: Verify SensitiveDataFilter masks API tokens inside string arguments."""
    caplog.set_level(logging.INFO)
    logger = logging.getLogger("test_mask_logger")
    logger.handlers.clear()
    logger.addFilter(SensitiveDataFilter(sensitive_strings={"my_super_secret_token"}))

    logger.info("Connecting with secret: %s", "my_super_secret_token")
    assert "my_super_secret_token" not in caplog.text
    assert "[MASKED_SECRET]" in caplog.text


def test_no_logging_error_on_httpx_style_record(caplog) -> None:
    """S3 Regression: Verify no TypeError occurs with dict or tuple args containing ints or floats."""
    caplog.set_level(logging.INFO)
    logger = logging.getLogger("test_record_logger")
    logger.handlers.clear()
    logger.addFilter(SensitiveDataFilter())

    logger.info("Order placed: status=%(code)d, price=%(price).2f, symbol=%(sym)s", {"code": 201, "price": 84123.45, "sym": "BTC/USDT"})
    assert "Logging error" not in caplog.text
    assert "status=201" in caplog.text


# --- S1 Tests ---
@pytest.mark.asyncio
async def test_resolver_calls_fetch_ohlcv_with_since(tmp_path) -> None:
    """S1 Regression: Verify OutcomeResolver can invoke exchange.fetch_ohlcv with since= keyword."""
    cfg = Settings(
        TRADING_MODE="paper",
        DB_PATH=str(tmp_path / "resolver_test.db"),
    )
    db = DatabaseManager(db_path=cfg.DB_PATH)
    try:
        db.save_reflex_decision(
            {
                "symbol": "BTC/USDT",
                "causal_timestamp_ms": 1700000000000,
                "regime": "trend",
                "direction": "LONG",
                "toxic_flow": False,
                "setup_quality": 3,
                "direction_confidence": 0.85,
                "risk_state": "NORMAL",
                "entry_price": 80000.0,
                "invalidation_price": 79000.0,
                "take_profit_price": 82000.0,
                "expected_reward_risk_ratio": 2.0,
                "rationale": "Test Decision",
            }
        )

        stub = StubCCXTClient()
        ex = BinanceExchangeWrapper(db=db, custom_settings=cfg, ccxt_client=stub)

        resolver = OutcomeResolver(db=db, exchange=ex, custom_settings=cfg)
        resolved_count = await resolver.resolve_pending_signals()
        assert resolved_count > 0, "Signal should be resolved once since parameter is accepted"
        outcomes = db._conn.execute("SELECT count(*) FROM signal_outcomes").fetchone()[0]
        assert outcomes > 0
    finally:
        db.close()


@pytest.mark.asyncio
async def test_resolver_continues_after_symbol_error(tmp_path) -> None:
    """S1: Verify that an error on one symbol does not crash resolution for subsequent symbols."""
    cfg = Settings(TRADING_MODE="paper", DB_PATH=str(tmp_path / "resolver_err_test.db"))
    db = DatabaseManager(db_path=cfg.DB_PATH)
    try:
        # First decision fails
        db.save_reflex_decision(
            {
                "symbol": "FAIL/USDT",
                "causal_timestamp_ms": 1700000000000,
                "regime": "trend",
                "direction": "LONG",
                "toxic_flow": False,
                "setup_quality": 3,
                "direction_confidence": 0.85,
                "risk_state": "NORMAL",
                "entry_price": 100.0,
                "invalidation_price": 95.0,
                "take_profit_price": 110.0,
                "expected_reward_risk_ratio": 2.0,
                "rationale": "Will Fail",
            }
        )
        # Second decision succeeds
        db.save_reflex_decision(
            {
                "symbol": "BTC/USDT",
                "causal_timestamp_ms": 1700000000000,
                "regime": "trend",
                "direction": "LONG",
                "toxic_flow": False,
                "setup_quality": 3,
                "direction_confidence": 0.85,
                "risk_state": "NORMAL",
                "entry_price": 80000.0,
                "invalidation_price": 79000.0,
                "take_profit_price": 82000.0,
                "expected_reward_risk_ratio": 2.0,
                "rationale": "Will Succeed",
            }
        )

        class FaultyStub(StubCCXTClient):
            async def fetch_ohlcv(self, symbol, timeframe="15m", since=None, limit=250):
                if "FAIL" in symbol:
                    raise RuntimeError("Exchange network timeout for FAIL/USDT")
                return await super().fetch_ohlcv(symbol, timeframe=timeframe, since=since, limit=limit)

        stub = FaultyStub()
        ex = BinanceExchangeWrapper(db=db, custom_settings=cfg, ccxt_client=stub)
        resolver = OutcomeResolver(db=db, exchange=ex, custom_settings=cfg)

        resolved_count = await resolver.resolve_pending_signals()
        assert resolved_count == 1
        assert int(db.get_state("resolver_errors") or 0) >= 1
    finally:
        db.close()


def test_resolver_sl_first_same_candle() -> None:
    """S1: Verify pure deterministic resolution enforces SL-first when high >= TP and low <= SL on same bar."""
    candles = [
        {"timestamp": 1700000900000, "open": 80000.0, "high": 83000.0, "low": 78000.0, "close": 81000.0, "volume": 10.0}
    ]
    res = OutcomeResolver.resolve_signal_with_candles(
        entry=80000.0,
        sl=79000.0,
        tp=82000.0,
        side="long",
        candles=candles,
        horizon_bars=12,
    )
    assert res["result"] == "SL", "When both SL and TP touch in the same bar, SL must touch first"
    assert res["r_gross"] == -1.0


@pytest.mark.asyncio
async def test_brier_uses_signal_outcomes(tmp_path) -> None:
    """S1: Verify OvernightBrierReviewer computes Brier score directly from signal_outcomes."""
    from review.overnight_review import OvernightBrierReviewer

    cfg = Settings(TRADING_MODE="paper", DB_PATH=str(tmp_path / "brier_outcomes_test.db"), MIN_BRIER_SAMPLES=5)
    db = DatabaseManager(db_path=cfg.DB_PATH)
    try:
        now_ms = int(time.time() * 1000)
        # Create 5 reflex decisions and their corresponding signal_outcomes
        for i in range(1, 6):
            dec_id = f"DEC_{i}"
            db.save_reflex_decision(
                {
                    "symbol": f"COIN{i}/USDT",
                    "causal_timestamp_ms": now_ms - 3600_000,
                    "regime": "trend",
                    "direction": "LONG",
                    "toxic_flow": False,
                    "setup_quality": 3,
                    "direction_confidence": 0.80,
                    "risk_state": "NORMAL",
                    "entry_price": 100.0,
                    "invalidation_price": 95.0,
                    "take_profit_price": 110.0,
                    "expected_reward_risk_ratio": 2.0,
                    "rationale": f"Decision {i}",
                }
            )
            # Result: TP
            db.save_signal_outcome(
                {
                    "decision_id": f"COIN{i}/USDT_{now_ms - 3600_000}",
                    "symbol": f"COIN{i}/USDT",
                    "strategy_id": "trend",
                    "side": "long",
                    "entry": 100.0,
                    "sl": 95.0,
                    "tp": 110.0,
                    "horizon_bars": 12,
                    "result": "TP",
                    "r_gross": 2.0,
                    "r_net": 1.95,
                    "bars_to_exit": 3,
                    "mae_r": 0.2,
                    "mfe_r": 2.0,
                    "regime": "trend",
                    "features": {},
                    "resolved_at_ms": now_ms - 1800_000,
                }
            )

        reviewer = OvernightBrierReviewer(db=db, custom_settings=cfg)
        review_res = await reviewer.run_overnight_review()
        assert review_res["status"] == "CALIBRATED"
        assert review_res["sample_count"] == 5
        assert review_res["brier_score"] is not None
    finally:
        db.close()


# --- S2 Test ---
@pytest.mark.asyncio
async def test_orphan_spot_with_existing_sl_is_adopted_no_critical(tmp_path) -> None:
    """S2 Regression: Verify orphan spot position with an active SL order on exchange is adopted into DB without critical errors or new orders."""
    cfg = Settings(
        TRADING_MODE="live",
        BINANCE_API_KEY="dummy_key",
        BINANCE_API_SECRET="dummy_secret",
        DB_PATH=str(tmp_path / "adoption_test.db"),
    )
    db = DatabaseManager(db_path=cfg.DB_PATH)
    try:
        class OrphanWithSLStub(StubCCXTClient):
            def __init__(self):
                super().__init__()
                self.created_orders = []

            async def fetch_balance(self):
                return {
                    "free": {"BTC": 0.0, "USDT": 50.0},
                    "total": {"BTC": 0.00015, "USDT": 50.0},
                    "used": {"BTC": 0.00015, "USDT": 0.0},
                }

            async def fetch_tickers(self):
                return {"BTC/USDT": {"last": 80000.0, "close": 80000.0}}

            async def fetch_open_orders(self, symbol=None):
                return [
                    {
                        "id": "68339261149",
                        "symbol": "BTC/USDT",
                        "type": "STOP_LOSS_LIMIT",
                        "side": "sell",
                        "amount": 0.00015,
                        "price": 78000.0,
                        "stopPrice": 78500.0,
                        "status": "open",
                    }
                ]

            async def create_order(self, *args, **kwargs):
                self.created_orders.append(kwargs)
                return {"id": "new_order_1"}

            async def create_market_sell_order(self, *args, **kwargs):
                self.created_orders.append(kwargs)
                return {"id": "new_market_sell_1"}

        stub = OrphanWithSLStub()
        ex = BinanceExchangeWrapper(db=db, custom_settings=cfg, ccxt_client=stub)
        reconciler = StateReconciler(db=db, exchange=ex, custom_settings=cfg)

        res = await reconciler.audit_and_reconcile_invariants()

        # In unpatched code, it fails to adopt, tries market-sell and marks entries_blocked=True
        assert res["entries_blocked"] is False
        assert len(stub.created_orders) == 0, "No new orders should be placed for already protected position"

        open_positions = db.get_open_positions()
        assert len(open_positions) == 1
        pos = open_positions[0]
        assert pos["symbol"] == "BTC/USDT"
        assert pos.get("adopted") is True
        assert pos.get("sl_exchange_order_id") == "68339261149"
    finally:
        db.close()


@pytest.mark.asyncio
async def test_orphan_futures_with_existing_algo_sl_is_adopted_not_closed(tmp_path) -> None:
    """S2: Verify orphan futures position with active SL order is adopted into DB, not closed."""
    cfg = Settings(TRADING_MODE="live", BINANCE_API_KEY="k", BINANCE_API_SECRET="s", DB_PATH=str(tmp_path / "fut_adopt.db"))
    db = DatabaseManager(db_path=cfg.DB_PATH)
    try:
        class FutOrphanStub(StubCCXTClient):
            def __init__(self):
                super().__init__()
                self.created_orders = []

            async def fetch_balance(self):
                return {"free": {"USDT": 100.0}, "total": {"USDT": 100.0}}

            async def fetch_active_futures_positions(self):
                return {
                    "BTC/USDT": {
                        "symbol": "BTC/USDT",
                        "contracts": 0.005,
                        "entryPrice": 80000.0,
                        "side": "long",
                        "leverage": 10,
                        "initialMargin": 40.0,
                        "notional": 400.0,
                    }
                }

            async def fetch_positions(self, symbols=None):
                return [
                    {
                        "symbol": "BTC/USDT:USDT",
                        "contracts": 0.005,
                        "entryPrice": 80000.0,
                        "side": "long",
                        "leverage": 10,
                        "initialMargin": 40.0,
                        "notional": 400.0,
                    }
                ]

            async def fetch_open_orders(self, symbol=None, params=None):
                return [
                    {
                        "id": "fut_sl_999",
                        "symbol": "BTC/USDT",
                        "type": "STOP_MARKET",
                        "side": "sell",
                        "amount": 0.005,
                        "stopPrice": 78400.0,
                        "reduceOnly": True,
                        "status": "open",
                    }
                ]

            async def create_futures_order(self, *args, **kwargs):
                self.created_orders.append(kwargs)
                return {"id": "fut_close_1"}

        stub = FutOrphanStub()
        ex = BinanceExchangeWrapper(db=db, custom_settings=cfg, ccxt_client=stub)
        ex.futures_client = stub
        reconciler = StateReconciler(db=db, exchange=ex, custom_settings=cfg)

        res = await reconciler.audit_and_reconcile_invariants()
        assert res["entries_blocked"] is False
        assert len(stub.created_orders) == 0, "Futures position with existing SL must NOT be closed"

        open_positions = db.get_open_positions()
        assert len(open_positions) == 1
        pos = open_positions[0]
        assert pos["symbol"] == "BTC/USDT"
        assert pos["market_type"] == "futures"
        assert pos.get("adopted") is True
        assert pos.get("sl_exchange_order_id") == "fut_sl_999"
    finally:
        db.close()


@pytest.mark.asyncio
async def test_orphan_without_sl_gets_sl_before_any_close(tmp_path) -> None:
    """S2: Verify orphan without existing SL attempts to place protective SL before any market close."""
    cfg = Settings(TRADING_MODE="live", BINANCE_API_KEY="k", BINANCE_API_SECRET="s", DB_PATH=str(tmp_path / "sl_first.db"))
    db = DatabaseManager(db_path=cfg.DB_PATH)
    try:
        class UnprotectedOrphanStub(StubCCXTClient):
            def __init__(self):
                super().__init__()
                self.orders = []

            async def fetch_balance(self):
                return {"free": {"ETH": 0.1, "USDT": 10.0}, "total": {"ETH": 0.1, "USDT": 10.0}}

            async def fetch_tickers(self):
                return {"ETH/USDT": {"last": 3000.0, "close": 3000.0}}

            async def fetch_open_orders(self, symbol=None, params=None):
                return []

            async def create_order(self, symbol, order_type, side, amount, price=None, params=None, client_order_id=None):
                order = {"id": "new_sl_123", "type": order_type, "side": side, "amount": amount, "price": price}
                self.orders.append(order)
                return order

        stub = UnprotectedOrphanStub()
        ex = BinanceExchangeWrapper(db=db, custom_settings=cfg, ccxt_client=stub)
        from engine.risk_engine import RiskEngine
        from engine.execution import ExecutionEngine
        risk = RiskEngine(db=db, custom_settings=cfg)
        execution = ExecutionEngine(exchange=ex, db=db, risk_engine=risk, custom_settings=cfg)
        reconciler = StateReconciler(db=db, exchange=ex, execution=execution, custom_settings=cfg)

        res = await reconciler.audit_and_reconcile_invariants()
        assert res["entries_blocked"] is False
        assert len(stub.orders) == 1
        assert "STOP" in stub.orders[0]["type"]
        assert stub.orders[0]["side"] == "sell"

        open_positions = db.get_open_positions()
        assert len(open_positions) == 1
        assert open_positions[0]["symbol"] == "ETH/USDT"
        assert open_positions[0]["adopted"] is True
    finally:
        db.close()


@pytest.mark.asyncio
async def test_dust_below_min_notional_is_ignored_once(tmp_path) -> None:
    """S2: Verify orphan token below dust_max is recorded as DUST_IGNORED without blocking entries."""
    cfg = Settings(TRADING_MODE="live", BINANCE_API_KEY="k", BINANCE_API_SECRET="s", DB_PATH=str(tmp_path / "dust.db"), DUST_MAX_USD=0.50)
    db = DatabaseManager(db_path=cfg.DB_PATH)
    try:
        class DustStub(StubCCXTClient):
            async def fetch_balance(self):
                return {"free": {"TRX": 1.0, "USDT": 10.0}, "total": {"TRX": 1.0, "USDT": 10.0}}

            async def fetch_tickers(self):
                return {"TRX/USDT": {"last": 0.15, "close": 0.15}}  # 1.0 * 0.15 = 0.15 USD < 0.50

            async def fetch_open_orders(self, symbol=None, params=None):
                return []

        stub = DustStub()
        ex = BinanceExchangeWrapper(db=db, custom_settings=cfg, ccxt_client=stub)
        reconciler = StateReconciler(db=db, exchange=ex, custom_settings=cfg)

        res = await reconciler.audit_and_reconcile_invariants()
        assert res["entries_blocked"] is False
        events = db.get_reconcile_events()
        assert any(e["event_type"] == "I1_ORPHAN_DUST_IGNORED" for e in events)
    finally:
        db.close()
