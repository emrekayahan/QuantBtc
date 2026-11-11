"""Section 9 Mandatory Regression Tests proving flaws in R1, R2, R3 before fixes."""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional
import pytest

from config.settings import Settings
from database.db_manager import DatabaseManager
from engine.exchange import BinanceExchangeWrapper
from engine.execution import ExecutionEngine
from engine.reconciler import StateReconciler
from engine.risk_engine import RiskEngine
from tests.test_idempotency import StubCCXTClient
from tests.test_kelly_and_risk import make_valid_decision


class RegressionStubClient(StubCCXTClient):
    """Stub keeping track of all write calls."""

    def __init__(self) -> None:
        super().__init__()
        self.cancels = 0

    async def cancel_order(self, order_id: str, symbol: Optional[str] = None) -> Dict[str, Any]:
        self.cancels += 1
        return {"id": order_id, "status": "canceled"}

    async def cancel_all_orders(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        self.cancels += 1
        return [{"id": "ALL", "status": "canceled"}]

    async def fetch_tickers(self) -> Dict[str, Any]:
        return {"BTC/USDT": {"last": 85000.0, "close": 85000.0}}


@pytest.mark.asyncio
async def test_regression_r1_panic_in_paper_does_not_call_exchange(tmp_path) -> None:
    """R1 regression: panic/kill switch in paper mode must never place real market sell or cancel orders."""
    cfg = Settings(
        TRADING_MODE="paper",
        BINANCE_API_KEY="dummy_key",
        BINANCE_API_SECRET="dummy_secret",
        DB_PATH=str(tmp_path / "p.db"),
    )
    db = DatabaseManager(db_path=cfg.DB_PATH)
    try:
        stub = RegressionStubClient()
        ex = BinanceExchangeWrapper(db=db, custom_settings=cfg, ccxt_client=stub)
        risk = RiskEngine(db=db, custom_settings=cfg, exchange=ex)
        eng = ExecutionEngine(exchange=ex, db=db, risk_engine=risk, custom_settings=cfg)

        await eng.execute_approved_entry(decision=make_valid_decision("BTC/USDT"), free_usdt=30.0)
        await risk.trigger_emergency_kill_switch("test")

        # Flaw: trigger_emergency_kill_switch calls exchange.create_market_sell_order unconditionally!
        assert stub.market_sell_calls == 0 and stub.cancels == 0
    finally:
        db.close()


@pytest.mark.asyncio
async def test_regression_r2_paper_equity_no_double_count(tmp_path) -> None:
    """R2 regression: opening a $11.50 paper position with $30 starting balance must keep equity ~$30, not $41.34."""
    cfg = Settings(
        TRADING_MODE="paper",
        PAPER_STARTING_USDT=30.0,
        BINANCE_API_KEY="dummy_key",
        BINANCE_API_SECRET="dummy_secret",
        DB_PATH=str(tmp_path / "p2.db"),
    )
    db = DatabaseManager(db_path=cfg.DB_PATH)
    try:
        stub = RegressionStubClient()
        ex = BinanceExchangeWrapper(db=db, custom_settings=cfg, ccxt_client=stub)
        risk = RiskEngine(db=db, custom_settings=cfg, exchange=ex)
        eng = ExecutionEngine(exchange=ex, db=db, risk_engine=risk, custom_settings=cfg)
        reconciler = StateReconciler(exchange=ex, db=db, execution=eng, custom_settings=cfg)

        await eng.execute_approved_entry(decision=make_valid_decision("BTC/USDT"), free_usdt=30.0)
        out = await reconciler.reconcile_cycle()

        # Flaw: total_equity = paper_wallet ($30) + daily_pnl ($0) + positions_value ($11.50) = $41.50
        assert 29.0 <= out["total_equity_usd"] <= 30.1
    finally:
        db.close()


@pytest.mark.asyncio
async def test_regression_r3_gross_exposure_includes_new_trade(tmp_path) -> None:
    """R3 regression: gross exposure cap must include the new trade's notional value."""
    cfg = Settings(
        TRADING_MODE="live",
        MAX_GROSS_EXPOSURE_X=1.0,
        MAX_OPEN_POSITIONS=5,
        MAX_CORRELATED_LONGS=5,
        BINANCE_API_KEY="dummy_key",
        BINANCE_API_SECRET="dummy_secret",
        DB_PATH=str(tmp_path / "p3.db"),
    )
    db = DatabaseManager(db_path=cfg.DB_PATH)
    try:
        stub = RegressionStubClient()
        ex = BinanceExchangeWrapper(db=db, custom_settings=cfg, ccxt_client=stub)
        risk = RiskEngine(db=db, custom_settings=cfg, exchange=ex)

        db.set_state("total_equity_usd", "30.0")
        # Add an open position with notional = 29.0 USD
        db.open_position({
            "position_id": "POS_EXISTING",
            "symbol": "ETH/USDT",
            "status": "OPEN",
            "entry_client_order_id": "QB_ETH",
            "entry_price": 2000.0,
            "current_price": 2000.0,
            "invalidation_price": 1950.0,
            "take_profit_price": 2100.0,
            "quantity": 0.0145,
            "cost_usd": 29.0,
            "notional_usd": 29.0,
        })

        dec = make_valid_decision("BTC/USDT")
        # Existing open notional is 29.0. New trade notional is >= 11.50. Total would be 40.50 > 30.0!
        g = risk.evaluate_gate(decision=dec, free_usdt=30.0, min_notional_usd=10.0)

        # Flaw: evaluate_gate checks if current_notional >= max_gross (29 < 30), ignoring new trade notional!
        assert g["approved"] is False
    finally:
        db.close()


def test_paper_pnl_persists_across_utc_days(tmp_path) -> None:
    """R2 test: paper ledger balances persist across daily PnL boundaries."""
    from database.db_manager import DatabaseManager
    db = DatabaseManager(db_path=str(tmp_path / "paper_ledger.db"))
    try:
        # Starting wallet: 30.0
        assert float(db.get_state("paper_wallet_balance_usdt", "0.0")) == 30.0
        # Buy trade deduction: $11.51
        db.record_paper_ledger("ENTRY_BUY_SPOT", -11.51, symbol="BTC/USDT")
        assert round(float(db.get_state("paper_wallet_balance_usdt", "0.0")), 2) == 18.49
        # Close trade profit return: $12.50
        db.record_paper_ledger("CLOSE_SPOT", 12.50, symbol="BTC/USDT")
        assert round(float(db.get_state("paper_wallet_balance_usdt", "0.0")), 2) == 30.99
        ledger = db.get_paper_ledger(limit=10)
        assert len(ledger) == 2
        assert ledger[0]["event"] == "CLOSE_SPOT"
        assert ledger[1]["event"] == "ENTRY_BUY_SPOT"
    finally:
        db.close()


def test_paper_limit_not_filled_if_low_not_touched() -> None:
    """R2 test: limit order is not filled if candle low does not reach order price."""
    from engine.paper_fill import PaperFillSimulator
    sim = PaperFillSimulator()
    # Limit buy at 95.0, candle low is 96.0 -> should not fill
    filled, price, qty = sim.simulate_limit_fill(
        order_price=95.0,
        candle_high=105.0,
        candle_low=96.0,
        candle_open=100.0,
        requested_qty=0.5,
    )
    assert filled is False
    assert qty == 0.0

    # Limit buy at 97.0, candle low is 96.0 -> touched, fills!
    filled, price, qty = sim.simulate_limit_fill(
        order_price=97.0,
        candle_high=105.0,
        candle_low=96.0,
        candle_open=100.0,
        requested_qty=0.5,
    )
    assert filled is True
    assert qty == 0.5
    assert price <= 97.05  # within slippage tolerance


def test_paper_sl_before_tp_same_candle() -> None:
    """R2 test: when both SL and TP are touched in the same candle, SL executes first."""
    from engine.paper_fill import PaperFillSimulator
    sim = PaperFillSimulator()
    # Entry at 100, SL at 95, TP at 110. Candle is 90 to 115 (touches both!)
    res = sim.simulate_candle_exit(
        entry_price=100.0,
        sl_price=95.0,
        tp_price=110.0,
        candle_high=115.0,
        candle_low=90.0,
        is_long=True,
    )
    assert res is not None
    assert res["triggered"] is True
    assert "STOP_LOSS" in res["reason"]
    assert res["conservative_sl_priority"] is True


def test_futures_leverage_respects_liq_buffer() -> None:
    """R3: isolated leverage must ensure liquidation distance >= 2x SL distance."""
    from engine.risk_sizer import RiskSizer
    cfg = Settings(LIQ_BUFFER_X=2.0, MAX_LEVERAGE=20)
    # SL distance = 10% (0.10). Required liq distance >= 20% + 0.5% maint margin = 20.5%.
    # 1/L >= 0.205 => L <= 4.87 => max allowed leverage is 4x!
    res = RiskSizer.calculate(
        equity=200.0,
        entry_price=100.0,
        invalidation_price=90.0,  # 10% SL
        min_notional=10.0,
        trading_mode="FUTURES",
        free_usdt=200.0,
        futures_free_usdt=50.0,
        open_positions=[],
        cfg=cfg,
    )
    assert res["approved"] is True
    assert res["leverage"] <= 4


def test_skip_when_min_notional_exceeds_risk_budget() -> None:
    """R3: large min_notional contracts exceeding risk exception budget must be rejected."""
    from engine.risk_sizer import RiskSizer
    cfg = Settings(
        SMALL_ACCOUNT_EQUITY_USD=100.0,
        SMALL_ACCOUNT_MAX_RISK_PCT=1.5,
        MIN_NOTIONAL_RISK_EXCEPTION_PCT=3.0,
    )
    # Equity $30. Max 3.0% risk exception budget = $0.90.
    # BTC contract with minNotional = $100 and SL distance 5% => risk = $5.0 >> $0.90!
    res = RiskSizer.calculate(
        equity=30.0,
        entry_price=50000.0,
        invalidation_price=47500.0,  # 5% SL
        min_notional=100.0,
        trading_mode="FUTURES",
        free_usdt=30.0,
        futures_free_usdt=30.0,
        open_positions=[],
        cfg=cfg,
    )
    assert res["approved"] is False
    assert res["reject_code"] == "MIN_NOTIONAL_EXCEEDS_RISK_BUDGET"


def test_small_account_min_notional_exception() -> None:
    """R3: small accounts relax risk budget up to 3% to clear minNotional."""
    from engine.risk_sizer import RiskSizer
    cfg = Settings(
        SMALL_ACCOUNT_EQUITY_USD=100.0,
        SMALL_ACCOUNT_MAX_RISK_PCT=1.5,
        MIN_NOTIONAL_RISK_EXCEPTION_PCT=3.0,
    )
    # Equity $30. 1.5% base budget = $0.45.
    # Contract minNotional = $10. SL = 2% ($0.20 risk + fees ~ $0.23).
    # $0.23 <= $0.90 (3% cap) -> approved under small account exception!
    res = RiskSizer.calculate(
        equity=30.0,
        entry_price=100.0,
        invalidation_price=98.0,  # 2% SL
        min_notional=10.0,
        trading_mode="SPOT",
        free_usdt=30.0,
        futures_free_usdt=0.0,
        open_positions=[],
        cfg=cfg,
    )
    assert res["approved"] is True
    assert res["position_usd"] >= 11.0


def test_margin_cap_per_trade() -> None:
    """R3: futures margin per trade cannot exceed MAX_MARGIN_PCT_PER_TRADE (default 25%)."""
    from engine.risk_sizer import RiskSizer
    cfg = Settings(MAX_MARGIN_PCT_PER_TRADE=25.0, MAX_LEVERAGE=1)
    # If leverage is 1x, margin = notional. If notional is $30 on $50 equity (60%), must be rejected!
    res = RiskSizer.calculate(
        equity=50.0,
        entry_price=100.0,
        invalidation_price=99.0,
        min_notional=25.0,
        trading_mode="FUTURES",
        free_usdt=50.0,
        futures_free_usdt=50.0,
        open_positions=[],
        cfg=cfg,
    )
    # Max margin allowed is 25% of 50 = $12.50. With 1x leverage, $25 margin exceeds $12.50.
    assert res["approved"] is False
    assert res["reject_code"] == "MARGIN_LIQ_CONFLICT"


@pytest.mark.asyncio
async def test_close_failure_does_not_mark_closed_and_rearms_sl(tmp_path) -> None:
    """R5 test: if market sell fails during position close, SL must be re-armed and position kept OPEN in DB."""
    class FailingSellStub(RegressionStubClient):
        def __init__(self):
            super().__init__()
            self.rearmed_orders = []

        async def create_order(self, symbol, order_type, side, amount, price=None, params=None):
            if order_type.lower() == "market" and side.lower() == "sell":
                raise RuntimeError("Binance 502 Bad Gateway on market sell")
            self.rearmed_orders.append((symbol, order_type, amount, price, params))
            return {"id": "REARMED_SL_123", "status": "open"}

    cfg = Settings(
        TRADING_MODE="live",
        BINANCE_API_KEY="dummy_key",
        BINANCE_API_SECRET="dummy_secret",
        DB_PATH=str(tmp_path / "close_fail.db"),
    )
    db = DatabaseManager(db_path=cfg.DB_PATH)
    try:
        stub = FailingSellStub()
        ex = BinanceExchangeWrapper(db=db, custom_settings=cfg, ccxt_client=stub)
        risk = RiskEngine(db=db, custom_settings=cfg, exchange=ex)
        eng = ExecutionEngine(exchange=ex, db=db, risk_engine=risk, custom_settings=cfg)

        pos_id = "POS_TEST_CLOSE_FAIL"
        db.open_position({
            "position_id": pos_id,
            "symbol": "BTC/USDT",
            "status": "OPEN",
            "entry_client_order_id": "QB_ENTRY",
            "sl_exchange_order_id": "OLD_SL_999",
            "entry_price": 50000.0,
            "current_price": 50000.0,
            "invalidation_price": 49000.0,
            "take_profit_price": 52000.0,
            "quantity": 0.001,
            "cost_usd": 50.0,
            "notional_usd": 50.0,
            "is_paper": False,
        })

        res = await eng.close_single_position_market(pos_id, reason="TEST_CLOSE")
        assert res["closed"] is False
        assert "SL rearmed: True" in res["reason"]

        # In DB, position must STILL be OPEN!
        open_pos = db.get_open_positions()
        assert len(open_pos) == 1
        assert open_pos[0]["position_id"] == pos_id
        assert open_pos[0]["status"] == "OPEN"
        assert open_pos[0]["sl_exchange_order_id"] == "REARMED_SL_123"
        assert len(stub.rearmed_orders) == 1
    finally:
        db.close()


@pytest.mark.asyncio
async def test_task_registry_rejects_entries_during_shutdown(tmp_path) -> None:
    """R5 test: TaskRegistry rejects new entries when shutdown begins."""
    from engine.task_registry import TaskRegistry
    reg = TaskRegistry()
    assert reg.accepting_entries is True
    reg.stop_accepting_entries()
    assert reg.accepting_entries is False

    cfg = Settings(
        TRADING_MODE="live",
        BINANCE_API_KEY="dummy_key",
        BINANCE_API_SECRET="dummy_secret",
        DB_PATH=str(tmp_path / "task_reg.db"),
    )
    db = DatabaseManager(db_path=cfg.DB_PATH)
    try:
        stub = RegressionStubClient()
        ex = BinanceExchangeWrapper(db=db, custom_settings=cfg, ccxt_client=stub)
        risk = RiskEngine(db=db, custom_settings=cfg, exchange=ex)
        eng = ExecutionEngine(exchange=ex, db=db, risk_engine=risk, custom_settings=cfg)
        eng.task_registry.stop_accepting_entries()

        res = await eng.execute_approved_entry(make_valid_decision("BTC/USDT"), free_usdt=50.0)
        assert res["executed"] is False
        assert res["reason"] == "SHUTDOWN_IN_PROGRESS"
    finally:
        db.close()


@pytest.mark.asyncio
async def test_panic_closes_futures_reduce_only(tmp_path) -> None:
    """R6 test: kill switch closes futures positions with reduceOnly=True and cancels futures SL."""
    class FuturesPanicStub(RegressionStubClient):
        def __init__(self):
            super().__init__()
            self.futures_orders = []
            self.futures_cancels = []

        async def cancel_order(self, id, symbol=None, params=None):
            self.futures_cancels.append((id, symbol, params))
            return {"id": id, "status": "canceled"}

        async def create_order(self, symbol, order_type, side, amount, price=None, params=None):
            self.futures_orders.append((symbol, order_type, side, amount, params))
            return {"id": "FUT_CLOSE_99", "status": "closed", "average": 50100.0}

    cfg = Settings(
        TRADING_MODE="live",
        ENABLE_FUTURES=True,
        BINANCE_API_KEY="dummy_key",
        BINANCE_API_SECRET="dummy_secret",
        DB_PATH=str(tmp_path / "panic_fut.db"),
    )
    db = DatabaseManager(db_path=cfg.DB_PATH)
    try:
        stub = FuturesPanicStub()
        ex = BinanceExchangeWrapper(db=db, custom_settings=cfg, ccxt_client=stub)
        ex.futures_client = stub
        risk = RiskEngine(db=db, custom_settings=cfg, exchange=ex)

        db.open_position({
            "position_id": "POS_FUT_1",
            "symbol": "BTC/USDT",
            "status": "OPEN",
            "entry_client_order_id": "QB_FUT_ENTRY",
            "sl_exchange_order_id": "SL_FUT_555",
            "entry_price": 50000.0,
            "current_price": 50100.0,
            "invalidation_price": 49000.0,
            "take_profit_price": 52000.0,
            "quantity": 0.05,
            "cost_usd": 2500.0,
            "margin_usd": 125.0,
            "notional_usd": 2500.0,
            "market_type": "futures",
            "is_paper": False,
        })

        out = await risk.trigger_emergency_kill_switch("TEST_FUTURES_PANIC")
        assert out["system_halted"] is True
        assert out["flattened_positions"] == 1
        assert len(stub.futures_orders) == 1
        # Verified reduceOnly=True on futures market sell
        sym, o_type, side, amt, params = stub.futures_orders[0]
        assert side == "sell"
        assert o_type == "market"
        assert params.get("reduceOnly") is True
        assert len(stub.futures_cancels) >= 1
    finally:
        db.close()


@pytest.mark.asyncio
async def test_panic_never_sells_unrelated_spot_balance(tmp_path) -> None:
    """R6 test: futures kill switch never executes market sells on spot balances."""
    class SpotCallTrackingStub(RegressionStubClient):
        def __init__(self):
            super().__init__()
            self.spot_market_sells = []

        async def create_order(self, symbol, order_type, side, amount, price=None, params=None):
            return {"id": "ORDER_1", "status": "closed", "average": 50000.0}

    cfg = Settings(
        TRADING_MODE="live",
        ENABLE_FUTURES=True,
        BINANCE_API_KEY="dummy_key",
        BINANCE_API_SECRET="dummy_secret",
        DB_PATH=str(tmp_path / "panic_no_spot.db"),
    )
    db = DatabaseManager(db_path=cfg.DB_PATH)
    try:
        spot_stub = SpotCallTrackingStub()
        fut_stub = SpotCallTrackingStub()
        ex = BinanceExchangeWrapper(db=db, custom_settings=cfg, ccxt_client=spot_stub)
        ex.futures_client = fut_stub
        risk = RiskEngine(db=db, custom_settings=cfg, exchange=ex)

        # Open futures position only
        db.open_position({
            "position_id": "POS_FUT_ONLY",
            "symbol": "BTC/USDT",
            "status": "OPEN",
            "entry_client_order_id": "QB_FUT_ENTRY",
            "entry_price": 50000.0,
            "current_price": 50000.0,
            "invalidation_price": 49000.0,
            "take_profit_price": 52000.0,
            "quantity": 0.01,
            "cost_usd": 500.0,
            "market_type": "futures",
            "is_paper": False,
        })

        await risk.trigger_emergency_kill_switch("PANIC_CHECK_NO_SPOT")
        # Spot stub must have 0 orders sent to it!
        assert len(spot_stub.spot_market_sells) == 0
        assert spot_stub.market_sell_calls == 0
    finally:
        db.close()


def test_dashboard_security_validation_rejects_weak_credentials() -> None:
    """R7 test: dashboard startup rejects empty, short, change_me, or admin credentials."""
    from dashboard.server import validate_dashboard_security_or_raise

    # 1. Reject empty password
    with pytest.raises(ValueError, match="cannot be empty"):
        validate_dashboard_security_or_raise(Settings(DASHBOARD_USERNAME="trader", DASHBOARD_PASSWORD=""))

    # 2. Reject change_me_* placeholder
    with pytest.raises(ValueError, match="default 'change_me"):
        validate_dashboard_security_or_raise(Settings(DASHBOARD_USERNAME="trader", DASHBOARD_PASSWORD="change_me_pass12345"))

    # 3. Reject password under 16 chars
    with pytest.raises(ValueError, match="too short"):
        validate_dashboard_security_or_raise(Settings(DASHBOARD_USERNAME="trader", DASHBOARD_PASSWORD="ShortPass123!"))

    # 4. Reject default admin username
    with pytest.raises(ValueError, match="cannot be 'admin'"):
        validate_dashboard_security_or_raise(Settings(DASHBOARD_USERNAME="admin", DASHBOARD_PASSWORD="VeryStrongSecurePass123456!"))

    # 5. Valid credentials pass
    validate_dashboard_security_or_raise(Settings(DASHBOARD_USERNAME="quant_operator", DASHBOARD_PASSWORD="VeryStrongSecurePass123456!"))


def test_log_filter_masks_secrets_and_keys() -> None:
    """R7 test: log filter masks OpenRouter keys, Telegram tokens, and secrets."""
    from config.log_filter import SensitiveDataFilter
    filt = SensitiveDataFilter({"my_super_secret_binance_key_12345"})

    dummy_token = "123456789:ABCdefGHIjklMNOpqrSTUvwxYZ_12345678"
    dummy_or = "sk-or-v1-" + "a" * 32 + "b" * 32
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg=f"Connecting with key my_super_secret_binance_key_12345 and telegram token {dummy_token} and OpenRouter {dummy_or}",
        args=(),
        exc_info=None,
    )
    filt.filter(record)
    assert "my_super_secret_binance_key_12345" not in record.msg
    assert "sk-or-v1-" not in record.msg
    assert "123456789:ABCdef" not in record.msg
    assert "[MASKED_SECRET]" in record.msg


class MockExchangeClientForReconcile(StubCCXTClient):
    """Stub simulating various exchange balances, orders, and positions for R4."""

    def __init__(self, spot_total=None, open_orders=None, fut_positions=None):
        super().__init__()
        self.spot_total = spot_total or {"USDT": 100.0}
        self._open_orders = open_orders or []
        self._fut_positions = fut_positions or []
        self.created_orders = []
        self.canceled_order_ids = []

    async def fetch_balance(self, params=None):
        return {
            "free": {k: v for k, v in self.spot_total.items()},
            "total": {k: v for k, v in self.spot_total.items()},
        }

    async def fetch_open_orders(self, symbol=None, since=None, limit=None, params=None):
        if symbol:
            return [o for o in self._open_orders if o.get("symbol") == symbol]
        return list(self._open_orders)

    async def fetch_positions(self, symbols=None, params=None):
        return list(self._fut_positions)

    async def create_order(self, symbol, order_type, side, amount, price=None, params=None):
        order = {
            "id": f"ORD_{len(self.created_orders) + 1}",
            "symbol": symbol,
            "type": order_type,
            "side": side,
            "amount": amount,
            "price": price,
            "status": "open" if "stop" in order_type.lower() else "closed",
            "filled": amount if "market" in order_type.lower() else 0.0,
            "params": params or {},
        }
        self.created_orders.append(order)
        return order

    async def cancel_order(self, order_id, symbol=None, params=None):
        self.canceled_order_ids.append(order_id)
        return {"id": order_id, "status": "canceled"}

    async def fetch_tickers(self, symbols=None, params=None):
        return {
            "BTC/USDT": {"last": 60000.0, "close": 60000.0},
            "ETH/USDT": {"last": 3000.0, "close": 3000.0},
            "SOL/USDT": {"last": 150.0, "close": 150.0},
        }


@pytest.mark.asyncio
async def test_r4_i1_orphan_position_gets_market_closed(tmp_path) -> None:
    """R4 (I1): Asset exists on exchange (> DUST_MAX_USD) but DB has no record -> market closed and logged."""
    cfg = Settings(
        TRADING_MODE="live",
        BINANCE_API_KEY="valid_live_api_key_12345",
        BINANCE_API_SECRET="valid_live_secret_12345",
        DB_PATH=str(tmp_path / "r4_i1.db"),
        DUST_MAX_USD=0.50,
    )
    db = DatabaseManager(db_path=cfg.DB_PATH)
    try:
        # User has 1.0 SOL on exchange (~$150), no DB position
        client = MockExchangeClientForReconcile(
            spot_total={"USDT": 100.0, "SOL": 1.0}
        )
        ex = BinanceExchangeWrapper(db=db, custom_settings=cfg, ccxt_client=client)
        risk = RiskEngine(db=db, custom_settings=cfg)
        exec_engine = ExecutionEngine(exchange=ex, db=db, risk_engine=risk, custom_settings=cfg)
        reconciler = StateReconciler(exchange=ex, db=db, execution=exec_engine, custom_settings=cfg)

        tickers = {"SOL/USDT": {"last": 150.0, "close": 150.0}}
        audit = await reconciler.audit_and_reconcile_invariants(tickers=tickers)

        # 1. Orphan SOL detected
        assert len(audit["violations"]) == 1
        assert audit["violations"][0]["invariant"] == "I1"
        assert audit["violations"][0]["symbol"] == "SOL/USDT"

        # 2. Market sell placed to close orphan
        assert len(client.created_orders) >= 1
        sell_order = client.created_orders[0]
        assert sell_order["symbol"] == "SOL/USDT"
        assert sell_order["side"] == "sell"
        assert sell_order["type"] == "market"

        # 3. Reconcile events recorded in DB
        events = db.get_reconcile_events()
        assert len(events) >= 1
        assert any("I1_ORPHAN" in e["event_type"] for e in events)
    finally:
        db.close()


@pytest.mark.asyncio
async def test_r4_i2_phantom_position_marked_closed(tmp_path) -> None:
    """R4 (I2): Position exists in DB marked OPEN, but exchange balance is 0 -> auto-remediated to CLOSED."""
    cfg = Settings(
        TRADING_MODE="live",
        BINANCE_API_KEY="valid_live_api_key_12345",
        BINANCE_API_SECRET="valid_live_secret_12345",
        DB_PATH=str(tmp_path / "r4_i2.db"),
    )
    db = DatabaseManager(db_path=cfg.DB_PATH)
    try:
        # Seed open position in DB for BTC/USDT
        db.open_position({
            "position_id": "POS_BTC_12345",
            "symbol": "BTC/USDT",
            "status": "OPEN",
            "quantity": 0.001,
            "entry_price": 60000.0,
            "current_price": 60000.0,
            "invalidation_price": 58000.0,
            "take_profit_price": 65000.0,
            "cost_usd": 60.0,
            "market_type": "spot",
        })

        # Exchange balance is 0 for BTC
        client = MockExchangeClientForReconcile(
            spot_total={"USDT": 100.0, "BTC": 0.0}
        )
        ex = BinanceExchangeWrapper(db=db, custom_settings=cfg, ccxt_client=client)
        risk = RiskEngine(db=db, custom_settings=cfg)
        exec_engine = ExecutionEngine(exchange=ex, db=db, risk_engine=risk, custom_settings=cfg)
        reconciler = StateReconciler(exchange=ex, db=db, execution=exec_engine, custom_settings=cfg)

        tickers = {"BTC/USDT": {"last": 60000.0, "close": 60000.0}}
        audit = await reconciler.audit_and_reconcile_invariants(tickers=tickers)

        # 1. Phantom position detected & resolved
        assert len(audit["violations"]) == 1
        assert audit["violations"][0]["invariant"] == "I2"
        assert len(audit["resolved"]) == 1

        # 2. Position in DB is now closed
        open_pos = db.get_open_positions()
        assert len(open_pos) == 0

        # 3. Reconcile events logged
        events = db.get_reconcile_events()
        assert any(e["event_type"] == "I2_PHANTOM_RESOLVED" for e in events)
    finally:
        db.close()


@pytest.mark.asyncio
async def test_r4_i3_i4_missing_or_dead_sl_is_rearmed(tmp_path) -> None:
    """R4 (I3/I4): Position is OPEN with balance, but SL on exchange is missing/dead -> rearm SL immediately."""
    cfg = Settings(
        TRADING_MODE="live",
        BINANCE_API_KEY="valid_live_api_key_12345",
        BINANCE_API_SECRET="valid_live_secret_12345",
        DB_PATH=str(tmp_path / "r4_sl.db"),
    )
    db = DatabaseManager(db_path=cfg.DB_PATH)
    try:
        # DB has open position with an old/dead SL order ID
        db.open_position({
            "position_id": "POS_ETH_12345",
            "symbol": "ETH/USDT",
            "status": "OPEN",
            "quantity": 0.05,
            "entry_price": 3000.0,
            "current_price": 3000.0,
            "invalidation_price": 2900.0,
            "take_profit_price": 3300.0,
            "cost_usd": 150.0,
            "sl_exchange_order_id": "OLD_DEAD_SL_ID",
            "market_type": "spot",
        })

        # Exchange has ETH balance, but NO open orders (SL died)
        client = MockExchangeClientForReconcile(
            spot_total={"USDT": 50.0, "ETH": 0.05},
            open_orders=[]
        )
        ex = BinanceExchangeWrapper(db=db, custom_settings=cfg, ccxt_client=client)
        risk = RiskEngine(db=db, custom_settings=cfg)
        exec_engine = ExecutionEngine(exchange=ex, db=db, risk_engine=risk, custom_settings=cfg)
        reconciler = StateReconciler(exchange=ex, db=db, execution=exec_engine, custom_settings=cfg)

        tickers = {"ETH/USDT": {"last": 3000.0, "close": 3000.0}}
        audit = await reconciler.audit_and_reconcile_invariants(tickers=tickers)

        # 1. I4 invariant violation detected & resolved
        assert len(audit["violations"]) == 1
        assert audit["violations"][0]["invariant"] == "I4"
        assert len(audit["resolved"]) == 1

        # 2. New Stop-loss order created on exchange
        sl_orders = [o for o in client.created_orders if "stop" in o["type"].lower() or "limit" in o["type"].lower()]
        assert len(sl_orders) >= 1
        new_sl = sl_orders[0]
        assert new_sl["symbol"] == "ETH/USDT"
        assert float(new_sl["amount"]) == 0.05

        # 3. Position in DB updated with new SL order id
        updated_pos = db.get_open_positions()[0]
        assert updated_pos["sl_exchange_order_id"] == new_sl["id"]
        assert updated_pos["sl_exchange_order_id"] != "OLD_DEAD_SL_ID"
    finally:
        db.close()


@pytest.mark.asyncio
async def test_r4_entries_blocked_until_reconciled(tmp_path) -> None:
    """R4: When reconciliation invariants are violated and unresolved, new entries are blocked in RiskEngine."""
    cfg = Settings(
        TRADING_MODE="live",
        BINANCE_API_KEY="valid_live_api_key_12345",
        BINANCE_API_SECRET="valid_live_secret_12345",
        DB_PATH=str(tmp_path / "r4_block.db"),
    )
    db = DatabaseManager(db_path=cfg.DB_PATH)
    try:
        # Set entries_blocked in DB
        db.set_state("entries_blocked", "true")

        risk = RiskEngine(db=db, custom_settings=cfg)
        decision = make_valid_decision()

        gate = risk.evaluate_gate(
            decision=decision,
            free_usdt=100.0,
        )

        assert gate["approved"] is False
        assert "temporarily blocked" in gate["reason"].lower()

        # Unblock entries
        db.set_state("entries_blocked", "false")
        gate_unblocked = risk.evaluate_gate(
            decision=decision,
            free_usdt=100.0,
        )
        assert gate_unblocked["approved"] is True
    finally:
        db.close()


@pytest.mark.asyncio
async def test_r10_signal_outcomes_sl_first_same_candle() -> None:
    """R10 test: When both SL and TP price levels are breached in the same candle, SL is considered hit FIRST."""
    from engine.outcome_resolver import OutcomeResolver

    entry = 100.0
    sl = 95.0
    tp = 110.0

    # Candle reaches low 94.0 (SL hit) and high 112.0 (TP hit)
    candle_same_bar = [
        {"timestamp": 1000, "open": 100.0, "high": 112.0, "low": 94.0, "close": 105.0, "volume": 1000.0}
    ]

    res = OutcomeResolver.resolve_signal_with_candles(
        entry=entry,
        sl=sl,
        tp=tp,
        side="long",
        candles=candle_same_bar,
        fee_pct=0.10,
        slippage_pct=0.05,
    )

    assert res["result"] == "SL"
    assert res["r_gross"] == -1.0
    assert res["r_net"] < -1.0  # Fees and slippage subtracted


@pytest.mark.asyncio
async def test_r11_brier_insufficient_data_changes_nothing(tmp_path) -> None:
    """R11 test: If samples < MIN_BRIER_SAMPLES (default 30), status is INSUFFICIENT_DATA and threshold is unchanged."""
    from review.overnight_review import OvernightBrierReviewer
    cfg = Settings(
        TRADING_MODE="paper",
        MIN_BRIER_SAMPLES=30,
        MIN_CONFIDENCE_THRESHOLD=0.68,
        MAX_POSITION_USD=25.0,
        DB_PATH=str(tmp_path / "brier_r11.db"),
    )
    db = DatabaseManager(db_path=cfg.DB_PATH)
    try:
        reviewer = OvernightBrierReviewer(db=db, custom_settings=cfg)
        res = await reviewer.run_overnight_review()

        assert res["status"] == "INSUFFICIENT_DATA"
        assert res["brier_score"] is None
        assert res["min_confidence_threshold"] == 0.68
        assert res["max_position_usd"] == 25.0

        # DB state must remain intact
        assert float(db.get_state("min_confidence_threshold")) == 0.68
    finally:
        db.close()


@pytest.mark.asyncio
async def test_r12_mistake_learner_uses_entry_trade_features(tmp_path) -> None:
    """R12 test: MistakeLearner uses pristine entry-time metrics recorded in trade_features, not latest state snapshot."""
    from engine.mistake_learner import MistakeLearner
    cfg = Settings(
        TRADING_MODE="paper",
        DB_PATH=str(tmp_path / "mistake_r12.db"),
    )
    db = DatabaseManager(db_path=cfg.DB_PATH)
    try:
        pos_id = "POS_TEST_FEATURE_123"
        # 1. Save entry-time feature snapshot where orderbook was heavily negative at entry (-0.35)
        db.save_trade_features({
            "position_id": pos_id,
            "strategy_id": "trend_momentum",
            "regime": "trending",
            "rsi": 45.0,
            "prev_rsi": 44.0,
            "adx": 25.0,
            "atr_pct": 1.2,
            "bb_dist_pct": 0.5,
            "ema_dist_pct": 0.2,
            "vol_ratio": 1.5,
            "ob_imbalance": -0.35,  # heavily negative at entry!
            "spread_bps": 2.0,
            "btc_trend_state": "trending_up",
            "hour_utc": 12,
            "side": "long",
            "leverage": 1,
        })

        # 2. Save a completely different latest state snapshot (RSI 75, OB +0.40)
        db.save_state_snapshot({
            "symbol": "BTC/USDT",
            "causal_timestamp_ms": int(time.time() * 1000),
            "regime": "trending",
            "mid_price": 60000.0,
            "best_bid": 59990.0,
            "best_ask": 60010.0,
            "spread_bps": 2.0,
            "orderbook_imbalance": 0.40,
            "closed_open": 59500.0,
            "closed_high": 60500.0,
            "closed_low": 59000.0,
            "closed_close": 60000.0,
            "closed_volume": 100.0,
            "vol_sma_20": 80.0,
            "rsi_14": 75.0,
            "prev_rsi_14": 74.0,
            "ema_9": 59800.0,
            "ema_21": 59500.0,
            "ema_50": 59000.0,
            "ema_200": 58000.0,
            "adx_14": 25.0,
            "atr_14": 500.0,
            "realized_vol": 0.02,
            "toxic_flow": "none",
        })

        learner = MistakeLearner(db=db)
        closed_pos = {
            "position_id": pos_id,
            "symbol": "BTC/USDT",
            "regime": "trending",
            "realized_pnl_usd": -1.50,
            "entry_price": 60000.0,
            "exit_price": 58800.0,
        }

        lesson = await learner.analyze_and_record_stop_loss(closed_pos=closed_pos)

        # MistakeLearner must pick up the entry-time OB imbalance (-0.35), NOT the latest snapshot (+0.40)
        assert lesson["orderbook_imbalance"] == -0.35
        assert "Satis baskisina karsi giris" in lesson["failure_reason"]
    finally:
        db.close()


@pytest.mark.asyncio
async def test_r14_zec_case_no_signal() -> None:
    """R14 test: ZEC-style wide BB distance (Low 1024 vs BB 953) must NOT trigger a Mean Reversion signal."""
    from engine.decision_reflex import DecisionReflexScorer
    from engine.schemas import StateSnapshotSchema

    scorer = DecisionReflexScorer(db=None)

    # Low is 1024 while BB lower is 953. In old code 1024 <= 953*1.012 failed, but 1024 vs 953 was too far
    state = StateSnapshotSchema(
        symbol="ZEC/USDT",
        causal_timestamp_ms=100000,
        regime="mean_reverting",
        mid_price=1030.0,
        best_bid=1029.0,
        best_ask=1031.0,
        spread_bps=2.0,
        orderbook_imbalance=0.10,
        closed_open=1035.0,
        closed_high=1040.0,
        closed_low=1024.0,  # 7.4% above bb_lower
        closed_close=1030.0,
        closed_volume=500.0,
        vol_sma_20=400.0,
        rsi_14=29.0,
        prev_rsi_14=28.0,
        ema_9=1035.0,
        ema_21=1045.0,
        ema_50=1060.0,
        ema_200=1100.0,
        bb_upper=1120.0,
        bb_mid=1036.0,
        bb_lower=953.0,     # Low is 1024, far from 953
        adx_14=22.0,
        atr_14=15.0,
        realized_vol=0.03,
        toxic_flow="none",
        open_positions_count=0,
        current_drawdown_pct=0.0,
    )

    decision = scorer.evaluate(state)
    assert decision.direction == "neutral"
    assert decision.setup_quality == 0


@pytest.mark.asyncio
async def test_r14_long_blocked_below_btc_ema200(tmp_path) -> None:
    """R14 test: When BTC is below its EMA200, long entries are blocked."""
    from engine.decision_reflex import DecisionReflexScorer
    from engine.schemas import StateSnapshotSchema
    cfg = Settings(
        BLOCK_LONGS_BELOW_BTC_EMA200=True,
        DB_PATH=str(tmp_path / "btc_block.db"),
    )
    db = DatabaseManager(db_path=cfg.DB_PATH)
    try:
        # Mark btc_below_ema200 as true
        db.set_state("btc_below_ema200", "true")

        scorer = DecisionReflexScorer(db=db, custom_settings=cfg)
        state = StateSnapshotSchema(
            symbol="ETH/USDT",
            causal_timestamp_ms=100000,
            regime="trending",
            mid_price=3000.0,
            best_bid=2999.0,
            best_ask=3001.0,
            spread_bps=2.0,
            orderbook_imbalance=0.15,
            closed_open=2950.0,
            closed_high=3020.0,
            closed_low=2940.0,
            closed_close=3000.0,
            closed_volume=1000.0,
            vol_sma_20=600.0,
            rsi_14=55.0,
            prev_rsi_14=54.0,
            ema_9=2980.0,
            ema_21=2950.0,
            ema_50=2900.0,
            ema_200=2800.0,
            bb_upper=3100.0,
            bb_mid=2950.0,
            bb_lower=2800.0,
            adx_14=30.0,
            atr_14=30.0,
            realized_vol=0.02,
            toxic_flow="none",
            open_positions_count=0,
            current_drawdown_pct=0.0,
        )

        decision = scorer.evaluate(state)
        assert decision.direction == "neutral"
        assert "below its ema200" in decision.rationale.lower()
    finally:
        db.close()


@pytest.mark.asyncio
async def test_r15_market_router_picks_spot_when_futures_not_better(tmp_path) -> None:
    """R15 test: MarketRouter picks Spot for long trades when spot cash is sufficient and costs are equal/better."""
    from engine.market_router import MarketRouter
    from tests.test_kelly_and_risk import make_valid_decision
    cfg = Settings(
        ENABLE_FUTURES=True,
        SPOT_FEE_PCT=0.10,
        FUTURES_FEE_PCT=0.05,
        DB_PATH=str(tmp_path / "router.db"),
    )
    db = DatabaseManager(db_path=cfg.DB_PATH)
    try:
        router = MarketRouter(db=db, custom_settings=cfg)
        decision = make_valid_decision(symbol="BTC/USDT")

        route = router.route_market_and_size(
            decision=decision,
            spot_free_usdt=100.0,
            futures_free_usdt=50.0,
            funding_rate_pct=0.01,
        )

        assert route["approved"] is True
        assert route["market_type"] == "SPOT"
        assert route["leverage"] == 1
    finally:
        db.close()


@pytest.mark.asyncio
async def test_r15_short_not_allowed_on_spot(tmp_path) -> None:
    """R15 test: Short direction is rejected if ENABLE_FUTURES is false."""
    from engine.market_router import MarketRouter
    from tests.test_kelly_and_risk import make_valid_decision
    cfg = Settings(
        ENABLE_FUTURES=False,
        DB_PATH=str(tmp_path / "router_short.db"),
    )
    db = DatabaseManager(db_path=cfg.DB_PATH)
    try:
        router = MarketRouter(db=db, custom_settings=cfg)
        dec = make_valid_decision(symbol="BTC/USDT")
        # Mutate to short
        short_dec = dec.model_copy(update={"direction": "short", "side": "short"})

        route = router.route_market_and_size(
            decision=short_dec,
            spot_free_usdt=100.0,
            futures_free_usdt=0.0,
        )

        assert route["approved"] is False
        assert "not allowed when enable_futures is false" in route["router_reason"].lower()
    finally:
        db.close()


@pytest.mark.asyncio
async def test_r17_autonomy_level_and_safe_mode(tmp_path) -> None:
    """R17 test: Verify autonomy ladder persistence and safe mode entry gate blocking."""
    cfg = Settings(
        TRADING_MODE="live",
        BINANCE_API_KEY="valid_key",
        BINANCE_API_SECRET="valid_secret",
        DB_PATH=str(tmp_path / "autonomy.db"),
    )
    db = DatabaseManager(db_path=cfg.DB_PATH)
    try:
        # Default level is 0
        assert db.get_autonomy_level() == 0

        # Promote to Level 1
        db.set_autonomy_level(1, "Met L0 graduation criteria")
        assert db.get_autonomy_level() == 1

        risk = RiskEngine(db=db, custom_settings=cfg)
        dec = make_valid_decision()

        # Normal gate check succeeds
        g1 = risk.evaluate_gate(dec, free_usdt=50.0)
        assert g1["approved"] is True

        # Activate Safe Mode
        db.set_safe_mode(True, reason="Clock drift anomaly > 800ms")
        assert db.is_safe_mode() is True

        # Safe Mode blocks new entries immediately
        g2 = risk.evaluate_gate(dec, free_usdt=50.0)
        assert g2["approved"] is False
        assert "safe mode" in g2["reason"].lower()

        # Safe Mode deactivated
        db.set_safe_mode(False)
        g3 = risk.evaluate_gate(dec, free_usdt=50.0)
        assert g3["approved"] is True
    finally:
        db.close()


@pytest.mark.asyncio
async def test_r18_executive_digest_generation(tmp_path) -> None:
    """R18 test: Verify executive digest markdown generation."""
    from review.executive_digest import ExecutiveDigest
    cfg = Settings(DB_PATH=str(tmp_path / "digest.db"))
    db = DatabaseManager(db_path=cfg.DB_PATH)
    try:
        db.set_state("total_equity_usd", "150.00")
        db.set_state("spot_free_usdt", "100.00")
        db.set_state("futures_free_usdt", "50.00")
        db.set_autonomy_level(1, "Initial test level")

        digest_gen = ExecutiveDigest(db=db, custom_settings=cfg)
        report = digest_gen.generate_digest_markdown()

        assert "GÜNLÜK BROKER RAPORU" in report
        assert "150.00" in report
        assert "L1" in report
        assert "HEALTHY" in report
    finally:
        db.close()









