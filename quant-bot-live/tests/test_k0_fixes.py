"""TDD Red->Green tests for V4 K0 (F0) urgent fixes."""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List

import pytest

from config.log_filter import SensitiveDataFilter
from config.settings import Settings
from database.db_manager import DatabaseManager
from engine.exchange import BinanceExchangeWrapper
from engine.execution import ExecutionEngine
from engine.outcome_resolver import OutcomeResolver
from engine.reconciler import StateReconciler
from engine.risk_engine import RiskEngine
from engine.scanner import AutonomousMarketScanner
from engine.schemas import ReflexDecisionSchema
from tests.test_idempotency import StubCCXTClient


def test_filter_masks_telegram_url_token() -> None:
    """K0-1: Telegram bot token inside httpx URL and Gemini key in URL must be masked."""
    filt = SensitiveDataFilter()
    tg_url = "HTTP Request: GET https://api.telegram.org/bot123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZ123456789/getUpdates?offset=1"
    masked_tg = filt.mask_text(tg_url)
    assert "123456789" not in masked_tg
    assert "ABCDEFGHIJKLMNOPQRSTUVWXYZ" not in masked_tg
    assert "[MASKED_SECRET]" in masked_tg

    gem_url = "HTTP Request: POST https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash-lite:generateContent?key=AQ.Ab8RN6IKYa0R9FVDRsDBqoit87wbycej6yet"
    masked_gem = filt.mask_text(gem_url)
    assert "AQ.Ab8RN6" not in masked_gem
    assert "[MASKED_SECRET]" in masked_gem


@pytest.mark.asyncio
async def test_all_scheduled_jobs_smoke(tmp_path, monkeypatch) -> None:
    """K0-2: Every scheduled job in main.py must run once with fake deps without raising NameError/AttributeError."""
    import sys
    import main as main_mod
    import database.db_manager as db_mod

    cfg = Settings(
        TRADING_MODE="paper",
        DB_PATH=str(tmp_path / "smoke.db"),
        TELEGRAM_BOT_TOKEN="",
        TELEGRAM_CHAT_ID="",
        OPENROUTER_API_KEY_1="",
        GEMINI_API_KEY="",
    )
    monkeypatch.setattr(sys.modules["config.settings"], "settings", cfg)
    monkeypatch.setattr(db_mod, "settings", cfg)
    monkeypatch.setattr(main_mod, "settings", cfg)

    orch = main_mod.AutonomousQuantEngineOrchestrator()
    try:
        # Stub network calls on exchange
        orch.exchange.client = StubCCXTClient()
        orch.exchange.futures_client = None

        async def _fake_tickers():
            return {"BTC/USDT": {"last": 68000.0, "close": 68000.0, "quoteVolume": 1e9, "percentage": 1.0, "bid": 67999.0, "ask": 68001.0}}

        async def _fake_ohlcv(*args, **kwargs):
            now = int(time.time() * 1000)
            return [[now - i * 900_000, 68000, 68100, 67900, 68000, 100] for i in range(250, 0, -1)]

        async def _fake_clock():
            return 5

        orch.exchange.fetch_tickers = _fake_tickers  # type: ignore[method-assign]
        orch.exchange.fetch_ohlcv = _fake_ohlcv  # type: ignore[method-assign]
        orch.exchange.fetch_ohlcv_range = _fake_ohlcv  # type: ignore[method-assign]
        orch.exchange.sync_clock_drift = _fake_clock  # type: ignore[method-assign]
        orch.db.update_heartbeat()

        # Run every scheduled job once
        await orch.safe_watchdog_job()
        await orch.safe_reconcile_job()
        await orch.safe_clock_sync_job()
        await orch.safe_scanner_job()
        await orch.evaluate_watchlist_reflex_cycle()
        await orch.safe_outcome_resolver_job()
        await orch.safe_overnight_brier_job()
        await orch.safe_daily_digest_job()

        # Also verify APScheduler EVENT_JOB_ERROR handler exists and triggers safe_mode after 3 errors
        assert hasattr(orch, "_on_scheduler_job_error"), "Missing APScheduler EVENT_JOB_ERROR listener"
        class FakeErrEvent:
            job_id = "safe_watchdog_job"
            exception = RuntimeError("boom")
            traceback = "tb"

        orch._on_scheduler_job_error(FakeErrEvent())
        orch._on_scheduler_job_error(FakeErrEvent())
        assert not orch.db.is_safe_mode()
        orch._on_scheduler_job_error(FakeErrEvent())
        assert orch.db.is_safe_mode()
    finally:
        orch.db.close()


@pytest.mark.asyncio
async def test_dust_spam_suppressed_and_logged_once(tmp_path, caplog) -> None:
    """K0-3: Dust balances are persisted in dust_ignored and logged INFO only on first seen or value increase."""
    cfg = Settings(
        TRADING_MODE="live",
        BINANCE_API_KEY="valid_key_123",
        BINANCE_API_SECRET="valid_sec_123",
        DB_PATH=str(tmp_path / "dust.db"),
        DUST_MAX_USD=0.50,
    )
    db = DatabaseManager(db_path=cfg.DB_PATH)
    try:
        client = StubCCXTClient()

        async def _dust_balance():
            return {"free": {"USDT": 50.0, "BTC": 0.000001}, "total": {"USDT": 50.0, "BTC": 0.000001}}

        client.fetch_balance = _dust_balance  # type: ignore[method-assign]
        ex = BinanceExchangeWrapper(db=db, custom_settings=cfg, ccxt_client=client)
        reconciler = StateReconciler(exchange=ex, db=db, custom_settings=cfg)
        tickers = {"BTC/USDT": {"last": 80000.0, "close": 80000.0}}  # $0.08 dust

        with caplog.at_level(logging.INFO, logger="engine.reconciler"):
            caplog.clear()
            await reconciler.audit_and_reconcile_invariants(tickers=tickers)
            first_count = sum(1 for r in caplog.records if "dust" in r.message.lower())
            assert first_count == 1

            caplog.clear()
            await reconciler.audit_and_reconcile_invariants(tickers=tickers)
            second_count = sum(1 for r in caplog.records if "dust" in r.message.lower())
            assert second_count == 0, f"Expected 0 dust INFO logs on repeat, got {second_count}"

        # Verify DB dust_ignored table exists and recorded BTC/USDT
        rows = db._conn.execute("SELECT symbol, value_usd FROM dust_ignored WHERE symbol = 'BTC/USDT'").fetchall()
        assert len(rows) == 1
        assert abs(rows[0][1] - 0.08) < 0.01
    finally:
        db.close()


@pytest.mark.asyncio
async def test_futures_min_notional_precheck_blocks_entry(tmp_path) -> None:
    """K0-4: Futures order with notional < market min_notional * 1.1 is rejected before hitting exchange."""
    cfg = Settings(
        TRADING_MODE="live",
        BINANCE_API_KEY="valid_key_123",
        BINANCE_API_SECRET="valid_sec_123",
        DB_PATH=str(tmp_path / "min_notional.db"),
        ENABLE_FUTURES=True,
        FUTURES_MIN_LEVEL=0,
        FUTURES_MIN_USDT=3.0,
        FUTURES_DEFAULT_LEVERAGE=2,
        MAX_POSITION_USD=5.20,  # below 5.0 * 1.1 = 5.50!
    )
    db = DatabaseManager(db_path=cfg.DB_PATH)
    try:
        client = StubCCXTClient()
        fut_client = StubCCXTClient()
        fut_client.markets = {
            "SOL/USDT:USDT": {
                "symbol": "SOL/USDT:USDT",
                "limits": {"cost": {"min": 5.0}, "amount": {"min": 0.01}},
                "precision": {"amount": 2, "price": 2},
            }
        }
        ex = BinanceExchangeWrapper(db=db, custom_settings=cfg, ccxt_client=client)
        ex.futures_client = fut_client
        risk = RiskEngine(db=db, custom_settings=cfg)
        exec_engine = ExecutionEngine(exchange=ex, db=db, risk_engine=risk, custom_settings=cfg)

        decision = ReflexDecisionSchema(
            symbol="SOL/USDT",
            causal_timestamp_ms=int(time.time() * 1000),
            regime="trending",
            direction="long",
            toxic_flow="none",
            setup_quality=3,
            direction_confidence=0.85,
            risk_state="safe",
            entry_price=100.0,
            invalidation_price=97.0,
            take_profit_price=106.0,
            expected_reward_risk_ratio=2.0,
            rationale="min_notional precheck test",
        )
        res = await exec_engine.execute_approved_entry(decision, free_usdt=0.0, futures_free_usdt=20.0)
        assert res["executed"] is False
        assert res.get("reject_reason") == "min_notional"
        assert len(fut_client.orders_by_client_id) == 0
    finally:
        db.close()


def test_peg_and_blacklist_symbols_filtered(tmp_path) -> None:
    """K0-5: Blacklisted tokens (U, USDC, FDUSD, XAUT, RWUSD) and peg/extreme-24h symbols are excluded."""
    cfg = Settings(DB_PATH=str(tmp_path / "scanner.db"))
    db = DatabaseManager(db_path=cfg.DB_PATH)
    try:
        scanner = AutonomousMarketScanner(exchange=BinanceExchangeWrapper(db=db, custom_settings=cfg), db=db, custom_settings=cfg)
        assert scanner.is_symbol_excluded("U/USDT", change_24h_pct=0.01) is True
        assert scanner.is_symbol_excluded("USDC/USDT", change_24h_pct=0.0) is True
        assert scanner.is_symbol_excluded("XAUT/USDT", change_24h_pct=0.5) is True
        assert scanner.is_symbol_excluded("PUMP/USDT", change_24h_pct=55.0) is True
        assert scanner.is_symbol_excluded("BTC/USDT", change_24h_pct=2.1) is False

        # Peg detector on 100 bars with tiny range (< 0.002)
        peg_ohlcv = [[i * 900_000, 1.0000, 1.0005, 0.9995, 1.0001, 1000] for i in range(100)]
        assert scanner.is_peg_candles(peg_ohlcv) is True
        normal_ohlcv = [[i * 900_000, 100.0, 101.2, 99.1, 100.5, 1000] for i in range(100)]
        assert scanner.is_peg_candles(normal_ohlcv) is False
    finally:
        db.close()


def test_signal_report_synthetic_10_signals(tmp_path) -> None:
    """K0-8: signal_report computes exact WinRate and R_net = R_gross - cost_R on 10 synthetic signals."""
    from tools.signal_report import generate_signal_report

    db_path = str(tmp_path / "report_test.db")
    db = DatabaseManager(db_path=db_path)
    try:
        now = int(time.time() * 1000)
        # Insert 10 synthetic signals: 4 TP (r_gross=+2.0, r_net=+1.85), 6 SL (r_gross=-1.0, r_net=-1.15)
        for i in range(10):
            is_tp = i < 4
            r_gross = 2.0 if is_tp else -1.0
            cost_r = 0.15
            r_net = r_gross - cost_r
            db.save_signal_outcome(
                {
                    "decision_id": f"DEC_{i}",
                    "symbol": "BTC/USDT",
                    "strategy_id": "test_strat",
                    "side": "long",
                    "entry": 100.0,
                    "sl": 98.0,
                    "tp": 104.0,
                    "horizon_bars": 16,
                    "result": "TP" if is_tp else "SL",
                    "r_gross": r_gross,
                    "r_net": r_net,
                    "bars_to_exit": 5,
                    "mae_r": 0.0,
                    "mfe_r": 2.0 if is_tp else 0.2,
                    "regime": "trending" if i < 5 else "high_vol",
                    "features": {"cost_r": cost_r},
                }
            )
    finally:
        db.close()

    report = generate_signal_report(since_str="3d", db_path=db_path)
    # Expected WinRate = 4 / 10 = 40.0%
    # Expected AvgGrossR = (4*2.0 + 6*-1.0)/10 = +0.2000 R
    # Expected AvgNetR = (4*1.85 + 6*-1.15)/10 = (7.4 - 6.9)/10 = +0.0500 R
    assert "40.0%" in report
    assert "+0.2000" in report
    assert "+0.0500" in report


def test_outcome_resolver_not_filled_limit_entry() -> None:
    """K0-9: Unfilled limit entry returns NOT_FILLED with 0.0 R when price never touches entry."""
    from engine.outcome_resolver import OutcomeResolver

    candles = [
        {"open": 102.0, "high": 105.0, "low": 101.0, "close": 104.0, "timestamp": 1000 + i * 900_000}
        for i in range(10)
    ]
    res = OutcomeResolver.resolve_signal_with_candles(
        entry=99.0,
        sl=96.0,
        tp=104.0,
        side="long",
        candles=candles,
        require_entry_touch=True,
    )
    assert res["result"] == "NOT_FILLED"
    assert res["r_gross"] == 0.0
    assert res["r_net"] == 0.0


def test_exchange_sets_clock_sync_and_recv_window(tmp_path) -> None:
    """K0-11: CCXT wrapper must configure adjustForTimeDifference=True and recvWindow=10000."""
    cfg = Settings(DB_PATH=str(tmp_path / "ex.db"), TRADING_MODE="paper")
    db = DatabaseManager(db_path=cfg.DB_PATH)
    try:
        ex = BinanceExchangeWrapper(db=db, custom_settings=cfg)
        opts = ex.client.options
        assert opts.get("adjustForTimeDifference") is True
        assert opts.get("recvWindow") == 10000
    finally:
        db.close()
