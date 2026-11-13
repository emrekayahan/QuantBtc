"""Phase F5 (K10 + K11 + K12) TDD tests: Volatility circuit breaker, BrokerReport, decision_log, ops."""
from __future__ import annotations

from pathlib import Path

import pytest

from database.db_manager import DatabaseManager


def test_volatility_circuit_breaker_blocks_extreme_btc_and_atr() -> None:
    """K10: VolatilityCircuitBreaker blocks entries when BTC 5m move > 1.5% or ATR% z-score > 3.0."""
    from engine.circuit_breaker import VolatilityCircuitBreaker

    cb = VolatilityCircuitBreaker()
    assert cb.check_entry_allowed(btc_5m_move_pct=0.4, atr_pct_zscore=1.0)["allowed"] is True

    r_btc = cb.check_entry_allowed(btc_5m_move_pct=1.8, atr_pct_zscore=1.0)
    assert r_btc["allowed"] is False
    assert "btc_5m" in r_btc["reason"]

    r_atr = cb.check_entry_allowed(btc_5m_move_pct=0.2, atr_pct_zscore=3.4)
    assert r_atr["allowed"] is False
    assert "atr_zscore" in r_atr["reason"]


def test_decision_log_and_daily_broker_report(tmp_path) -> None:
    """K11: Structured decision_log records bot leverage/risk choices and BrokerReporter formats daily report."""
    from engine.broker_report import BrokerReporter

    db = DatabaseManager(db_path=str(tmp_path / "broker.db"))
    try:
        rep = BrokerReporter(db=db)
        rep.log_decision(
            decision_id="DEC_1",
            strategy_id="trend_pullback",
            symbol="BTC/USDT",
            side="long",
            risk_pct=0.5,
            leverage=5,
            stop_dist_pct=1.2,
            reason="learn_mode_cap_5x_liq_safe",
        )
        rows = db._conn.execute("SELECT strategy_id, leverage, reason FROM decision_log WHERE decision_id = 'DEC_1'").fetchall()
        assert len(rows) == 1
        assert rows[0][1] == 5

        report = rep.format_daily_broker_report(
            total_equity=100.0,
            futures_equity=95.0,
            spot_equity=5.0,
            daily_net_pnl=1.25,
            weekly_net_pnl=3.50,
        )
        assert "Broker Raporu" in report
        assert "trend_pullback" in report
        assert "5x" in report
    finally:
        db.close()


def test_deploy_service_example_has_graceful_limits() -> None:
    """K12: deploy/quant-bot.service.example defines TimeoutStopSec=90 and MemoryHigh/Max."""
    svc = Path(__file__).resolve().parent.parent / "deploy" / "quant-bot.service.example"
    assert svc.exists()
    txt = svc.read_text(encoding="utf-8")
    assert "TimeoutStopSec=90" in txt
    assert "MemoryHigh=" in txt
    assert "MemoryMax=" in txt
