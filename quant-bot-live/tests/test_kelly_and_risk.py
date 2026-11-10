"""Unit tests for Phase 4: Quarter-Kelly cap, hard risk gate, and Emergency Kill-Switch."""

from __future__ import annotations

import pytest

from database.db_manager import DatabaseManager
from engine.risk_engine import RiskEngine
from engine.schemas import ReflexDecisionSchema


def make_valid_decision(
    symbol: str = "BTC/USDT",
    confidence: float = 0.88,
    setup_quality: int = 3,
    regime: str = "trending",
    toxic_flow: str = "none",
    risk_state: str = "safe",
    direction: str = "long",
) -> ReflexDecisionSchema:
    return ReflexDecisionSchema(
        symbol=symbol,
        causal_timestamp_ms=1700000900000,
        regime=regime,  # type: ignore[arg-type]
        direction=direction,  # type: ignore[arg-type]
        toxic_flow=toxic_flow,  # type: ignore[arg-type]
        setup_quality=setup_quality,
        direction_confidence=confidence,
        risk_state=risk_state,  # type: ignore[arg-type]
        entry_price=85000.0,
        invalidation_price=83725.0,  # 1.5% stop
        take_profit_price=88187.5,  # 2.5 R:R
        expected_reward_risk_ratio=2.5,
        rationale="Unit test valid setup",
    )


def test_quarter_kelly_strictly_capped_at_025_and_max_position_usd(tmp_path) -> None:
    """Verify Quarter-Kelly never exceeds 0.25 of Free USDT and is bounded by MAX_POSITION_USD ($25)."""
    db = DatabaseManager(db_path=str(tmp_path / "risk_test.db"))
    try:
        risk = RiskEngine(db=db)
        f_q = risk.calculate_quarter_kelly_fraction(
            direction_confidence=0.95,
            expected_reward_risk_ratio=3.0,
        )
        assert 0.0 < f_q <= 0.25

        dec = make_valid_decision(confidence=0.90)
        # With $500 free USDT, 0.25 * 500 = $125, which must be capped at MAX_POSITION_USD ($25.0)
        gate_res = risk.evaluate_gate(dec, free_usdt=500.0, min_notional_usd=10.0)
        assert gate_res["approved"] is True
        assert gate_res["position_usd"] == 25.0

        # With $30.59 free Spot USDT, Spot min position floor ($11.50) ensures it clears Binance MIN_NOTIONAL
        gate_spot_30 = risk.evaluate_gate(dec, free_usdt=30.59, min_notional_usd=10.0)
        assert gate_spot_30["approved"] is True
        assert gate_spot_30["position_usd"] == 11.50
        assert gate_spot_30["trading_mode"] == "SPOT"

        # With $9.0 free Spot USDT (< $11.0 minNotional*1.10), rejected
        gate_small = risk.evaluate_gate(dec, free_usdt=9.0, min_notional_usd=10.0)
        assert gate_small["approved"] is False

        # When Futures wallet has >= $5.0 USDT, auto-detects FUTURES 2x-3x Isolated mode
        gate_fut = risk.evaluate_gate(
            dec, free_usdt=30.59, min_notional_usd=10.0, futures_free_usdt=10.0
        )
        assert gate_fut["approved"] is True
        assert gate_fut["trading_mode"] == "FUTURES"
        assert gate_fut["leverage"] in (2, 3)
    finally:
        db.close()


def test_hard_risk_gate_rejects_crisis_toxic_flow_and_low_confidence(tmp_path) -> None:
    """Verify every non-negotiable gate condition rejects invalid proposals."""
    db = DatabaseManager(db_path=str(tmp_path / "gate_test.db"))
    try:
        risk = RiskEngine(db=db)

        assert not risk.evaluate_gate(make_valid_decision(regime="crisis"), free_usdt=500.0)["approved"]
        assert not risk.evaluate_gate(
            make_valid_decision(toxic_flow="spoofing_suspected"), free_usdt=500.0
        )["approved"]
        assert not risk.evaluate_gate(make_valid_decision(confidence=0.62), free_usdt=500.0)["approved"]
        assert risk.evaluate_gate(make_valid_decision(confidence=0.70, setup_quality=2), free_usdt=500.0)["approved"]
        assert not risk.evaluate_gate(make_valid_decision(setup_quality=1), free_usdt=500.0)["approved"]
        assert not risk.evaluate_gate(make_valid_decision(risk_state="near_limit"), free_usdt=500.0)[
            "approved"
        ]
        assert not risk.evaluate_gate(make_valid_decision(direction="neutral"), free_usdt=500.0)[
            "approved"
        ]
    finally:
        db.close()


def test_daily_max_trades_cap_and_mistake_learner(tmp_path) -> None:
    """Verify DAILY_MAX_TRADES = 2 cap blocks 3rd trade and MistakeLearner records lessons."""
    from engine.mistake_learner import MistakeLearner
    from engine.scanner import AutonomousMarketScanner

    assert AutonomousMarketScanner.is_eligible_usdt_spot_symbol("BTC/USDT") is True
    assert AutonomousMarketScanner.is_eligible_usdt_spot_symbol("BNB/USDT") is True
    assert AutonomousMarketScanner.is_eligible_usdt_spot_symbol("NVDAB/USDT") is False
    assert AutonomousMarketScanner.is_eligible_usdt_spot_symbol("SNDKB/USDT") is False

    db = DatabaseManager(db_path=str(tmp_path / "cap_test.db"))
    try:
        risk = RiskEngine(db=db)
        dec = make_valid_decision(symbol="SOL/USDT", confidence=0.74, setup_quality=2)

        # Open and close 2 positions today
        for idx in (1, 2):
            pid = f"POS_SOL_{idx}"
            db.open_position(
                {
                    "position_id": pid,
                    "symbol": f"COIN{idx}/USDT",
                    "entry_client_order_id": f"QB_{idx}",
                    "entry_price": 100.0,
                    "current_price": 100.0,
                    "invalidation_price": 98.0,
                    "take_profit_price": 105.0,
                    "quantity": 0.12,
                    "cost_usd": 12.0,
                }
            )
            db.close_position(pid, exit_price=101.0, close_reason="TAKE_PROFIT_HIT")

        assert db.get_daily_trades_count() == 2
        gate_3rd = risk.evaluate_gate(dec, free_usdt=30.59)
        assert gate_3rd["approved"] is False
        assert "Daily trade cap" in gate_3rd["reason"]

        learner = MistakeLearner(db=db)
        db.add_trade_lesson(
            {
                "id": "lesson_test_1",
                "symbol": "ETH/USDT",
                "regime": "mean_reverting",
                "entry_rsi": 30.5,
                "orderbook_imbalance": -0.18,
                "pnl": -0.45,
                "failure_reason": "Test SL lesson",
                "confidence_penalty": 0.08,
            }
        )
        eval_res = learner.evaluate_setup_against_lessons("ETH/USDT", "mean_reverting", 30.0, -0.15)
        assert eval_res["allowed"] is False
    finally:
        db.close()


@pytest.mark.asyncio
async def test_armed_emergency_kill_switch_halts_and_flattens(tmp_path) -> None:
    """Verify triggering Kill-Switch sets system_halted=True and closes open positions."""
    db = DatabaseManager(db_path=str(tmp_path / "kill_test.db"))
    try:
        db.open_position(
            {
                "position_id": "POS_BTCUSDT_1",
                "symbol": "BTC/USDT",
                "entry_client_order_id": "QB_BTCUSDT_1",
                "entry_exchange_order_id": "101",
                "sl_client_order_id": "QBSL_BTCUSDT_1",
                "sl_exchange_order_id": "102",
                "entry_price": 80000.0,
                "current_price": 81000.0,
                "invalidation_price": 78800.0,
                "take_profit_price": 83000.0,
                "quantity": 0.0003,
                "cost_usd": 24.0,
            }
        )
        risk = RiskEngine(db=db, exchange=None)
        res = await risk.trigger_emergency_kill_switch(reason="Test Emergency")
        assert res["system_halted"] is True
        assert res["flattened_positions"] == 1
        assert db.is_system_halted() is True
        assert len(db.get_open_positions()) == 0
    finally:
        db.close()
