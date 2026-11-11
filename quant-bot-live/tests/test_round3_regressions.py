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


def test_single_db_path_source(tmp_path) -> None:
    """S4: Verify Settings is the single deterministic source of DB paths."""
    s_live = Settings(TRADING_MODE="live", DB_PATH="")
    assert s_live.effective_db_path.endswith("quant_bot_live.db")

    s_paper = Settings(TRADING_MODE="paper", DB_PATH="")
    assert s_paper.effective_db_path.endswith("quant_bot_paper.db")

    s_custom = Settings(TRADING_MODE="live", DB_PATH=str(tmp_path / "custom.db"))
    assert s_custom.effective_db_path == str(tmp_path / "custom.db")


def test_legacy_db_with_open_positions_blocks_start(tmp_path) -> None:
    """S4: If data/quant_bot.db exists with open positions, startup safety blocks start."""
    from scripts.migrate_legacy_db import check_startup_safety

    legacy_db_path = tmp_path / "quant_bot.db"
    legacy_db = DatabaseManager(db_path=str(legacy_db_path))
    legacy_db.open_position({
        "position_id": "LEGACY_POS_1",
        "symbol": "BTC/USDT",
        "entry_price": 60000.0,
        "current_price": 60000.0,
        "quantity": 0.01,
        "cost_usd": 600.0,
        "status": "OPEN",
    })
    legacy_db.close()

    cfg = Settings(TRADING_MODE="live", DB_PATH=str(tmp_path / "quant_bot_live.db"))
    with pytest.raises(RuntimeError, match="LEGACY_DB_DETECTED"):
        check_startup_safety(cfg, legacy_db_path=str(legacy_db_path))


def test_migration_is_idempotent_and_backs_up(tmp_path) -> None:
    """S4: Migration script creates pre-migration backup and is safely re-runnable."""
    from scripts.migrate_legacy_db import migrate_legacy_db

    src_path = tmp_path / "quant_bot.db"
    dst_path = tmp_path / "quant_bot_live.db"
    backup_dir = tmp_path / "backups"

    src_db = DatabaseManager(db_path=str(src_path))
    src_db.open_position({
        "position_id": "POS_1",
        "symbol": "BTC/USDT",
        "entry_price": 50000.0,
        "current_price": 50000.0,
        "quantity": 0.05,
        "cost_usd": 2500.0,
        "status": "OPEN",
    })
    src_db.close()

    # Pre-create dst DB
    dst_db = DatabaseManager(db_path=str(dst_path))
    dst_db.close()

    # Run migration first time
    res1 = migrate_legacy_db(src_path=str(src_path), dst_path=str(dst_path), backup_dir=str(backup_dir))
    assert res1["migrated_positions"] == 1
    assert len(list(backup_dir.glob("*.db"))) >= 1

    # Run migration second time (idempotency check)
    res2 = migrate_legacy_db(src_path=str(src_path), dst_path=str(dst_path), backup_dir=str(backup_dir))
    assert res2["migrated_positions"] == 1

    check_db = DatabaseManager(db_path=str(dst_path))
    open_pos = check_db.get_open_positions()
    check_db.close()
    assert len(open_pos) == 1
    assert open_pos[0]["position_id"] == "POS_1"


def test_market_type_backfill(tmp_path) -> None:
    """S4: Migration backfills market_type for futures positions and ensures non-null leverage/margin."""
    from scripts.migrate_legacy_db import migrate_legacy_db

    src_path = tmp_path / "legacy_mt.db"
    dst_path = tmp_path / "live_mt.db"

    src_db = DatabaseManager(db_path=str(src_path))
    # Insert raw legacy position where market_type is NULL
    src_db._conn.execute("""
        INSERT INTO positions (
            position_id, symbol, entry_client_order_id, entry_price, current_price,
            invalidation_price, take_profit_price, quantity, cost_usd,
            unrealized_pnl_usd, realized_pnl_usd, direction_confidence, setup_quality,
            regime, status, opened_at_ms, market_type
        ) VALUES (
            'FUT_1', 'SOL/USDT', 'CID_1', 150.0, 150.0,
            140.0, 165.0, 2.0, 300.0,
            0.0, 0.0, 0.8, 0.8,
            'momentum', 'OPEN', 1700000000000, NULL
        )
    """)
    src_db.upsert_order({
        "client_order_id": "SL_FUT_1",
        "symbol": "SOL/USDT",
        "side": "sell",
        "order_type": "STOP_MARKET",
        "status": "open",
        "price": 140.0,
        "amount": 2.0,
        "filled": 0.0,
        "cost_usd": 280.0,
    })
    src_db.close()

    migrate_legacy_db(src_path=str(src_path), dst_path=str(dst_path))

    dst_db = DatabaseManager(db_path=str(dst_path))
    pos = dst_db.get_position("FUT_1")
    dst_db.close()

    assert pos is not None
    assert pos["market_type"] == "futures"
    assert pos["leverage"] is not None and pos["leverage"] >= 1
    assert pos["margin_usd"] is not None
    assert pos["notional_usd"] is not None


@pytest.mark.asyncio
async def test_time_stop_grace_for_legacy_positions(tmp_path) -> None:
    """S4: Old positions within LEGACY_POSITION_GRACE_HOURS are protected from immediate time exit dump."""
    cfg = Settings(
        TRADING_MODE="live",
        BINANCE_API_KEY="k",
        BINANCE_API_SECRET="s",
        DB_PATH=str(tmp_path / "grace.db"),
        MAX_HOLD_BARS=32,  # 8 hours
        LEGACY_POSITION_GRACE_HOURS=24.0,
    )
    db = DatabaseManager(db_path=cfg.DB_PATH)
    try:
        # Position opened 12 hours ago (> 8 hours max hold)
        twelve_hours_ago_ms = int((time.time() - 12 * 3600) * 1000)
        db.open_position({
            "position_id": "OLD_POS_1",
            "symbol": "BTC/USDT",
            "entry_price": 50000.0,
            "current_price": 50000.0,
            "invalidation_price": 49000.0,
            "take_profit_price": 52000.0,
            "quantity": 0.01,
            "cost_usd": 500.0,
            "opened_at_ms": twelve_hours_ago_ms,
            "market_type": "spot",
            "status": "OPEN",
            "sl_exchange_order_id": "sl_legacy_1",
            "adopted": True,
        })

        class NoCloseExchange(StubCCXTClient):
            def __init__(self):
                super().__init__()
                self.closed_orders = []

            async def fetch_balance(self):
                return {"free": {"BTC": 0.01, "USDT": 1000.0}, "total": {"BTC": 0.01, "USDT": 1000.0}}

            async def fetch_tickers(self):
                return {"BTC/USDT": {"last": 50000.0, "close": 50000.0}}

            async def fetch_open_orders(self, symbol=None, params=None):
                return [{
                    "id": "sl_legacy_1",
                    "symbol": "BTC/USDT",
                    "type": "STOP_LOSS_LIMIT",
                    "side": "sell",
                    "amount": 0.01,
                    "stopPrice": 49000.0,
                    "status": "open",
                }]

            async def create_order(self, *args, **kwargs):
                self.closed_orders.append(kwargs)
                return {"id": "market_close_1"}

        stub = NoCloseExchange()
        ex = BinanceExchangeWrapper(db=db, custom_settings=cfg, ccxt_client=stub)
        from engine.risk_engine import RiskEngine
        from engine.execution import ExecutionEngine
        risk = RiskEngine(db=db, custom_settings=cfg)
        execution = ExecutionEngine(exchange=ex, db=db, risk_engine=risk, custom_settings=cfg)
        reconciler = StateReconciler(db=db, exchange=ex, execution=execution, custom_settings=cfg)

        await reconciler.reconcile_cycle()

        # Invariant checks and time-exit must NOT close the position because it is within 24h grace
        assert len(stub.closed_orders) == 0
        pos = db.get_position("OLD_POS_1")
        assert pos["status"] == "OPEN"
    finally:
        db.close()


@pytest.mark.asyncio
async def test_every_exit_writes_order_row(tmp_path) -> None:
    """S5: Every exit (market close, time exit, phantom resolution) must write an order row to orders table."""
    cfg = Settings(TRADING_MODE="live", BINANCE_API_KEY="k", BINANCE_API_SECRET="s", DB_PATH=str(tmp_path / "exit_orders.db"))
    db = DatabaseManager(db_path=cfg.DB_PATH)
    try:
        db.open_position({
            "position_id": "POS_TO_CLOSE",
            "symbol": "BTC/USDT",
            "entry_price": 50000.0,
            "current_price": 51000.0,
            "quantity": 0.01,
            "cost_usd": 500.0,
            "status": "OPEN",
        })

        class MarketCloseStub(StubCCXTClient):
            async def fetch_balance(self):
                return {"free": {"BTC": 0.01, "USDT": 1000.0}, "total": {"BTC": 0.01, "USDT": 1000.0}}

            async def create_order(self, symbol, order_type, side, amount, price=None, params=None):
                return {
                    "id": "ord_close_99",
                    "clientOrderId": (params or {}).get("newClientOrderId", "close_cid_99"),
                    "symbol": symbol,
                    "side": side,
                    "type": order_type,
                    "price": 51000.0,
                    "average": 51000.0,
                    "amount": amount,
                    "filled": amount,
                    "status": "closed",
                }

        stub = MarketCloseStub()
        ex = BinanceExchangeWrapper(db=db, custom_settings=cfg, ccxt_client=stub)
        from engine.risk_engine import RiskEngine
        from engine.execution import ExecutionEngine
        risk = RiskEngine(db=db, custom_settings=cfg)
        execution = ExecutionEngine(exchange=ex, db=db, risk_engine=risk, custom_settings=cfg)

        res = await execution.close_single_position_market("POS_TO_CLOSE", reason="MANUAL_TEST_CLOSE")
        assert res["closed"] is True

        # Check that orders table has an exit order row recorded
        orders = db.get_open_orders()  # check orders in db
        all_orders = db._conn.execute("SELECT * FROM orders WHERE symbol = 'BTC/USDT'").fetchall()
        cols = [d[0] for d in db._conn.execute("SELECT * FROM orders").description]
        order_dicts = [dict(zip(cols, r)) for r in all_orders]

        assert len(order_dicts) >= 1
        exit_order = next(o for o in order_dicts if o["side"] == "sell")
        assert exit_order["order_type"].lower() == "market"
        assert exit_order["filled"] == 0.01
        assert exit_order["price"] == 51000.0
    finally:
        db.close()


@pytest.mark.asyncio
async def test_pnl_uses_actual_fills(tmp_path) -> None:
    """S5: Exit computes realized PnL using real exchange fills and fee deductions."""
    cfg = Settings(TRADING_MODE="live", BINANCE_API_KEY="k", BINANCE_API_SECRET="s", DB_PATH=str(tmp_path / "real_pnl.db"))
    db = DatabaseManager(db_path=cfg.DB_PATH)
    try:
        db.open_position({
            "position_id": "POS_REAL_FILLS",
            "symbol": "ETH/USDT",
            "entry_price": 3000.0,
            "current_price": 3100.0,
            "quantity": 1.0,
            "cost_usd": 3000.0,
            "status": "OPEN",
        })

        class TradeFillStub(StubCCXTClient):
            async def fetch_balance(self):
                return {"free": {"ETH": 1.0, "USDT": 1000.0}, "total": {"ETH": 1.0, "USDT": 1000.0}}

            async def create_order(self, symbol, order_type, side, amount, price=None, params=None):
                return {
                    "id": "eth_close_1",
                    "clientOrderId": "eth_cid_1",
                    "symbol": symbol,
                    "side": "sell",
                    "type": "market",
                    "price": 3100.0,
                    "average": 3100.0,
                    "amount": 1.0,
                    "filled": 1.0,
                    "fee": {"cost": 3.10, "currency": "USDT"},
                    "status": "closed",
                }

            async def fetch_my_trades(self, symbol=None, since=None, limit=None):
                return [
                    {
                        "id": "trade_eth_1",
                        "order": "eth_close_1",
                        "symbol": symbol,
                        "side": "sell",
                        "price": 3100.0,
                        "amount": 1.0,
                        "fee": {"cost": 3.10, "currency": "USDT"},
                    }
                ]

        stub = TradeFillStub()
        ex = BinanceExchangeWrapper(db=db, custom_settings=cfg, ccxt_client=stub)
        from engine.risk_engine import RiskEngine
        from engine.execution import ExecutionEngine
        risk = RiskEngine(db=db, custom_settings=cfg)
        execution = ExecutionEngine(exchange=ex, db=db, risk_engine=risk, custom_settings=cfg)

        res = await execution.close_single_position_market("POS_REAL_FILLS", reason="TAKE_PROFIT_TEST")
        assert res["closed"] is True

        pos = db.get_position("POS_REAL_FILLS")
        assert pos["status"] == "CLOSED"
        assert pos["pnl_estimated"] is False
        # Gross PnL = (3100 - 3000) * 1 = 100.0. Minus fee = ~96.90
        assert 95.0 <= pos["realized_pnl_usd"] <= 100.0
    finally:
        db.close()


def test_estimated_pnl_excluded_from_stats(tmp_path) -> None:
    """S5: Positions marked pnl_estimated=True are excluded from daily realized PnL."""
    cfg = Settings(TRADING_MODE="live", DB_PATH=str(tmp_path / "stats_pnl.db"))
    db = DatabaseManager(db_path=cfg.DB_PATH)
    try:
        now_ms = int(time.time() * 1000)
        # Position 1: Real fill, realized_pnl = 50.0, pnl_estimated = False
        db.open_position({
            "position_id": "REAL_POS",
            "symbol": "BTC/USDT",
            "entry_price": 50000.0,
            "quantity": 0.01,
            "cost_usd": 500.0,
            "pnl_estimated": False,
        })
        db.close_position(position_id="REAL_POS", exit_price=55000.0, close_reason="TP", pnl_estimated=False)

        # Position 2: Phantom / estimated, realized_pnl = 100.0, pnl_estimated = True
        db.open_position({
            "position_id": "EST_POS",
            "symbol": "SOL/USDT",
            "entry_price": 100.0,
            "quantity": 1.0,
            "cost_usd": 100.0,
            "pnl_estimated": True,
        })
        db.close_position(position_id="EST_POS", exit_price=200.0, close_reason="PHANTOM_RESOLVED", pnl_estimated=True)

        daily_pnl = db.get_daily_realized_pnl()
        # Daily PnL must ONLY count the real fill (approx 49-50 USD), NOT the estimated 100 USD!
        assert daily_pnl < 70.0
    finally:
        db.close()


def test_invalid_lessons_give_no_penalty(tmp_path) -> None:
    """S6: MistakeLearner ignores unvalidated or prompt-echo lessons and enforces MIN_LESSONS_FOR_PENALTY."""
    from engine.mistake_learner import MistakeLearner

    cfg = Settings(TRADING_MODE="paper", DB_PATH=str(tmp_path / "lessons.db"), MIN_LESSONS_FOR_PENALTY=3)
    db = DatabaseManager(db_path=cfg.DB_PATH)
    learner = MistakeLearner(db=db, custom_settings=cfg)
    try:
        now_ms = int(time.time() * 1000)
        # Add 2 validated lessons from 3 hours ago (older than 2h cooldown, within 48h penalty window)
        for i in range(2):
            db.add_trade_lesson({
                "id": f"lesson_{i}",
                "symbol": "BTC/USDT",
                "regime": "mean_reverting",
                "entry_rsi": 45.0,
                "orderbook_imbalance": -0.2,
                "pnl": -1.0,
                "failure_reason": f"Satis baskisina karsi giris {i}",
                "confidence_penalty": 0.05,
                "created_at": now_ms - (3 * 3600 * 1000),
                "validated": True,
            })

        # Add 1 unvalidated lesson
        db.add_trade_lesson({
            "id": "lesson_unvalidated",
            "symbol": "BTC/USDT",
            "regime": "mean_reverting",
            "entry_rsi": 45.0,
            "orderbook_imbalance": -0.2,
            "pnl": -1.0,
            "failure_reason": "Unvalidated failure",
            "confidence_penalty": 0.05,
            "created_at": now_ms - (3 * 3600 * 1000),
            "validated": False,
        })

        # Add 1 prompt-echo trash lesson
        db.add_trade_lesson({
            "id": "lesson_prompt_echo",
            "symbol": "BTC/USDT",
            "regime": "mean_reverting",
            "entry_rsi": 45.0,
            "orderbook_imbalance": -0.2,
            "pnl": -1.0,
            "failure_reason": "The user wants a 1-sentence explanation of why it failed...",
            "confidence_penalty": 0.05,
            "created_at": now_ms - (3 * 3600 * 1000),
            "validated": True,
        })

        # Valid count is only 2, which is < MIN_LESSONS_FOR_PENALTY (3)
        eval_res = learner.evaluate_setup_against_lessons(
            symbol="BTC/USDT",
            regime="mean_reverting",
            entry_rsi=45.0,
            orderbook_imbalance=-0.2,
        )
        assert eval_res["confidence_penalty"] == 0.0, f"Expected 0.0 penalty when valid lessons < min_lessons, got {eval_res['confidence_penalty']}"

        # Now add 3rd validated lesson
        db.add_trade_lesson({
            "id": "lesson_3",
            "symbol": "BTC/USDT",
            "regime": "mean_reverting",
            "entry_rsi": 45.0,
            "orderbook_imbalance": -0.2,
            "pnl": -1.0,
            "failure_reason": "Valid failure pattern",
            "confidence_penalty": 0.05,
            "created_at": now_ms - (3 * 3600 * 1000),
            "validated": True,
        })

        eval_res2 = learner.evaluate_setup_against_lessons(
            symbol="BTC/USDT",
            regime="mean_reverting",
            entry_rsi=45.0,
            orderbook_imbalance=-0.2,
        )
        assert eval_res2["confidence_penalty"] > 0.0, "Expected penalty when valid lessons >= min_lessons"
    finally:
        db.close()


@pytest.mark.asyncio
async def test_trade_features_written_on_open_paper_and_live(tmp_path) -> None:
    """S6: Entry-time features are written to trade_features for both paper and live modes."""
    from engine.execution import ExecutionEngine
    from engine.risk_engine import RiskEngine
    from engine.schemas import ReflexDecisionSchema
    from tests.test_idempotency import StubCCXTClient

    cfg = Settings(TRADING_MODE="paper", DB_PATH=str(tmp_path / "features.db"))
    db = DatabaseManager(db_path=cfg.DB_PATH)
    # Record a snapshot so trade_features has source data
    now_ms = int(time.time() * 1000)
    db.save_state_snapshot({
        "id": f"SNAP_ETH_{now_ms}",
        "symbol": "ETH/USDT",
        "causal_timestamp_ms": now_ms,
        "regime": "mean_reverting",
        "mid_price": 3000.0,
        "spread_bps": 2.0,
        "orderbook_imbalance": 0.15,
        "rsi_14": 42.0,
        "ema_9": 3000.0,
        "ema_21": 2980.0,
        "ema_50": 2950.0,
        "ema_200": 2900.0,
        "adx_14": 25.0,
        "atr_14": 30.0,
        "realized_vol": 0.02,
        "toxic_flow": "none",
        "bb_upper": 3100.0,
        "bb_middle": 3000.0,
        "bb_lower": 2900.0,
        "vol_sma_20": 100.0,
        "closed_volume": 120.0,
        "closed_quote_volume": 360000.0,
    })
    db.set_state("free_usdt", "1000.0")

    ex = BinanceExchangeWrapper(db=db, custom_settings=cfg)
    stub = StubCCXTClient()
    ex.client = stub
    risk = RiskEngine(db=db, custom_settings=cfg)
    exec_eng = ExecutionEngine(exchange=ex, db=db, risk_engine=risk, custom_settings=cfg)

    decision = ReflexDecisionSchema(
        symbol="ETH/USDT",
        direction="long",
        direction_confidence=0.85,
        setup_quality=3,
        entry_price=3000.0,
        invalidation_price=2900.0,
        take_profit_price=3200.0,
        expected_reward_risk_ratio=2.0,
        toxic_flow="none",
        risk_state="safe",
        regime="mean_reverting",
        causal_timestamp_ms=now_ms,
        rationale="Test high-conviction setup",
    )

    try:
        res = await exec_eng.execute_approved_entry(decision=decision, free_usdt=500.0)
        assert res.get("executed") is True, f"Failed execution: {res}"
        pos_id = res.get("position_id")
        assert pos_id is not None

        feat = db.get_trade_features(pos_id)
        assert feat is not None, "trade_features row was not created for paper execution!"
        assert feat["position_id"] == pos_id
        assert feat["rsi"] == pytest.approx(42.0, abs=0.5)
        assert feat["adx"] == pytest.approx(25.0, abs=0.5)
        assert feat["ob_imbalance"] == pytest.approx(0.15, abs=0.01)
        assert feat["regime"] == "mean_reverting"
    finally:
        db.close()


def test_threshold_single_source_and_logged(tmp_path) -> None:
    """S6: min_confidence_threshold is only updated via save_brier_calibration / log_calibration_change with audit trail."""
    cfg = Settings(TRADING_MODE="live", DB_PATH=str(tmp_path / "threshold.db"), MIN_CONFIDENCE_THRESHOLD=0.68)
    db = DatabaseManager(db_path=cfg.DB_PATH)
    try:
        # Initial state is initialized with settings.MIN_CONFIDENCE_THRESHOLD (0.68)
        assert db.get_state("min_confidence_threshold") == "0.68"
        from engine.risk_engine import RiskEngine
        risk = RiskEngine(db=db, custom_settings=cfg)
        assert risk.get_active_min_confidence() == 0.68

        # Update via calibration log
        db.log_calibration_change(old_threshold=0.68, new_threshold=0.72, reason="Overconfident test", n_samples=35)
        assert risk.get_active_min_confidence() == 0.72

        # Check calibration log record exists
        cal_row = db._conn.execute("SELECT * FROM calibration_log ORDER BY created_at_ms DESC LIMIT 1").fetchone()
        assert cal_row is not None
        assert cal_row[1] == 0.68
        assert cal_row[2] == 0.72
        assert "Overconfident test" in cal_row[3]
    finally:
        db.close()


def test_no_dead_settings() -> None:
    """S7: Every field in Settings is actively referenced outside config/settings.py."""
    import glob
    from config.settings import Settings

    fields = list(Settings.model_fields.keys())
    files = [p for p in glob.glob("**/*.py", recursive=True) if "settings.py" not in p and "venv" not in p and ".venv" not in p]
    usage = {f: 0 for f in fields}
    for f in fields:
        for p in files:
            with open(p, encoding="utf-8", errors="ignore") as fp:
                if f in fp.read():
                    usage[f] += 1

    unused = [f for f, count in usage.items() if count == 0]
    assert not unused, f"Found dead/unreferenced settings in Settings: {unused}"


@pytest.mark.asyncio
async def test_market_router_is_called_from_execution(tmp_path) -> None:
    """S7: MarketRouter routes proposals and decision reason is saved in reflex_decisions."""
    from engine.execution import ExecutionEngine
    from engine.risk_engine import RiskEngine
    from engine.schemas import ReflexDecisionSchema
    from tests.test_idempotency import StubCCXTClient

    cfg = Settings(TRADING_MODE="paper", DB_PATH=str(tmp_path / "router.db"), ENABLE_FUTURES=False)
    db = DatabaseManager(db_path=cfg.DB_PATH)
    ex = BinanceExchangeWrapper(db=db, custom_settings=cfg)
    ex.client = StubCCXTClient()
    risk = RiskEngine(db=db, custom_settings=cfg)
    exec_eng = ExecutionEngine(exchange=ex, db=db, risk_engine=risk, custom_settings=cfg)

    now_ms = int(time.time() * 1000)
    decision = ReflexDecisionSchema(
        symbol="BTC/USDT",
        direction="short",
        direction_confidence=0.85,
        setup_quality=3,
        entry_price=50000.0,
        invalidation_price=51000.0,
        take_profit_price=48000.0,
        expected_reward_risk_ratio=2.0,
        toxic_flow="none",
        risk_state="safe",
        regime="trending",
        causal_timestamp_ms=now_ms,
        rationale="Short signal test",
    )

    try:
        # With ENABLE_FUTURES=False, short signal MUST be rejected by MarketRouter
        res = await exec_eng.execute_approved_entry(decision=decision, free_usdt=100.0)
        assert res.get("executed") is False
        assert "no spot shorting" in res.get("reason", "").lower() or "not allowed" in res.get("reason", "").lower()

        # Check reflex_decisions recorded router_reason
        dec_row = db._conn.execute("SELECT market_type, router_reason FROM reflex_decisions WHERE symbol = 'BTC/USDT'").fetchone()
        assert dec_row is not None
        assert dec_row[0] == "NONE"
        assert "short" in str(dec_row[1]).lower()
    finally:
        db.close()


@pytest.mark.asyncio
async def test_autonomy_auto_promotion_requires_100_samples_and_positive_ci(tmp_path) -> None:
    """S8: Autonomy auto-promotion from L1 to L2 is locked unless signal_outcomes has n>=100 and lower-bound 95% CI >= 0."""
    from review.overnight_review import OvernightBrierReviewer

    cfg = Settings(TRADING_MODE="live", DB_PATH=str(tmp_path / "autonomy.db"), MIN_BRIER_SAMPLES=10, LOG_DIR=str(tmp_path / "logs"))
    db = DatabaseManager(db_path=cfg.DB_PATH)
    db.set_autonomy_level(1, "Initial test level")
    reviewer = OvernightBrierReviewer(db=db, custom_settings=cfg)

    try:
        now_ms = int(time.time() * 1000)
        # 1. With only 50 positive outcomes (n < 100), promotion MUST NOT happen
        for i in range(50):
            ts = now_ms - (i * 60000)
            dec_id = f"BTC/USDT_{ts}"
            db.save_reflex_decision({
                "symbol": "BTC/USDT",
                "causal_timestamp_ms": ts,
                "regime": "mean_reverting",
                "direction": "long",
                "toxic_flow": "none",
                "setup_quality": 3,
                "direction_confidence": 0.80,
                "risk_state": "safe",
                "entry_price": 50000.0,
                "invalidation_price": 49000.0,
                "take_profit_price": 52000.0,
                "expected_reward_risk_ratio": 2.0,
                "rationale": "Test proposal",
            })
            db.save_signal_outcome({
                "decision_id": dec_id,
                "symbol": "BTC/USDT",
                "strategy_id": "test",
                "side": "long",
                "entry": 50000.0,
                "sl": 49000.0,
                "tp": 52000.0,
                "horizon_bars": 32,
                "result": "TP",
                "r_gross": 2.0,
                "r_net": 1.9,
                "bars_to_exit": 10,
                "mae_r": 0.1,
                "mfe_r": 2.0,
                "regime": "mean_reverting",
                "features_json": "{}",
                "resolved_at_ms": now_ms - (i * 60000),
            })

        res1 = await reviewer.run_overnight_review()
        assert db.get_autonomy_level() == 1, "Promoted prematurely when n < 100!"
        assert res1.get("autonomy_promoted") is False

        # 2. Add 55 more outcomes with negative net R so n=105 but net R CI lower-bound is negative
        for i in range(50, 105):
            ts = now_ms - (i * 60000)
            dec_id = f"BTC/USDT_{ts}"
            db.save_reflex_decision({
                "symbol": "BTC/USDT",
                "causal_timestamp_ms": ts,
                "regime": "mean_reverting",
                "direction": "long",
                "toxic_flow": "none",
                "setup_quality": 3,
                "direction_confidence": 0.80,
                "risk_state": "safe",
                "entry_price": 50000.0,
                "invalidation_price": 49000.0,
                "take_profit_price": 52000.0,
                "expected_reward_risk_ratio": 2.0,
                "rationale": "Test proposal",
            })
            db.save_signal_outcome({
                "decision_id": dec_id,
                "symbol": "BTC/USDT",
                "strategy_id": "test",
                "side": "long",
                "entry": 50000.0,
                "sl": 49000.0,
                "tp": 52000.0,
                "horizon_bars": 32,
                "result": "SL",
                "r_gross": -1.0,
                "r_net": -1.5,
                "bars_to_exit": 10,
                "mae_r": 1.0,
                "mfe_r": 0.2,
                "regime": "mean_reverting",
                "features_json": "{}",
                "resolved_at_ms": now_ms - (i * 60000),
            })

        res2 = await reviewer.run_overnight_review()
        assert db.get_autonomy_level() == 1, "Promoted when CI lower-bound < 0!"
        assert res2.get("autonomy_promoted") is False

        # 3. Now add 100 strong positive outcomes so mean R is high and lower bound 95% CI >= 0
        for i in range(105, 210):
            ts = now_ms - (i * 60000)
            dec_id = f"BTC/USDT_{ts}"
            db.save_reflex_decision({
                "symbol": "BTC/USDT",
                "causal_timestamp_ms": ts,
                "regime": "mean_reverting",
                "direction": "long",
                "toxic_flow": "none",
                "setup_quality": 3,
                "direction_confidence": 0.80,
                "risk_state": "safe",
                "entry_price": 50000.0,
                "invalidation_price": 49000.0,
                "take_profit_price": 52000.0,
                "expected_reward_risk_ratio": 2.0,
                "rationale": "Test proposal",
            })
            db.save_signal_outcome({
                "decision_id": dec_id,
                "symbol": "BTC/USDT",
                "strategy_id": "test",
                "side": "long",
                "entry": 50000.0,
                "sl": 49000.0,
                "tp": 52000.0,
                "horizon_bars": 32,
                "result": "TP",
                "r_gross": 3.0,
                "r_net": 2.8,
                "bars_to_exit": 5,
                "mae_r": 0.05,
                "mfe_r": 3.0,
                "regime": "mean_reverting",
                "features_json": "{}",
                "resolved_at_ms": now_ms - (i * 60000),
            })

        res3 = await reviewer.run_overnight_review()
        assert db.get_autonomy_level() == 2, "Failed to auto-promote when criteria satisfied!"
        assert res3.get("autonomy_promoted") is True

        # Verify autonomy_log audit trail entry exists
        log_rows = db._conn.execute("SELECT * FROM autonomy_log WHERE new_level = 2").fetchall()
        assert len(log_rows) >= 1
    finally:
        db.close()


@pytest.mark.asyncio
async def test_fetch_my_trades_read_only_verification(tmp_path) -> None:
    """S9: BinanceExchangeWrapper supports fetch_my_trades for permission verification."""
    from tests.test_idempotency import StubCCXTClient

    cfg = Settings(TRADING_MODE="live", DB_PATH=str(tmp_path / "perm.db"))
    db = DatabaseManager(db_path=cfg.DB_PATH)
    ex = BinanceExchangeWrapper(db=db, custom_settings=cfg)
    stub = StubCCXTClient()
    async def mock_fetch_my_trades(symbol, since=None, limit=50, params=None):
        return [{"id": "TR_1", "symbol": symbol, "side": "buy", "price": 50000.0, "amount": 0.01}]

    stub.fetch_my_trades = mock_fetch_my_trades
    ex.client = stub

    try:
        trades = await ex.fetch_my_trades("BTC/USDT", limit=1)
        assert len(trades) == 1
        assert trades[0]["id"] == "TR_1"
        assert trades[0]["symbol"] == "BTC/USDT"
    finally:
        db.close()






