"""Phase F4 (K7 + K8 + K9) TDD tests: Learning v3, LearningBudget, Thompson bandit, Autonomy v2."""
from __future__ import annotations

import time
from typing import Any, Dict, List

import pytest

from config.settings import Settings
from database.db_manager import DatabaseManager


# ---------------------------------------------------------------------------
# K7: Learning v3 Tests
# ---------------------------------------------------------------------------

def test_posterior_shrinks_small_n() -> None:
    """K7: Small n shrinks strongly toward weak negative prior (-0.05 R); large n converges to sample mean."""
    from engine.learning_v3 import LearningEngineV3

    le = LearningEngineV3()
    # 2 lucky +2.0R trades should NOT give +2.0R posterior mean due to shrinkage
    p_small = le.compute_posterior([2.0, 2.0])
    assert p_small["mu_mean"] < 0.5
    assert p_small["mu_lcb"] < p_small["mu_mean"]

    # 100 trades averaging +0.30R -> converges close to +0.30R with P(mu>0) > 0.90
    p_large = le.compute_posterior([0.30] * 100)
    assert p_large["mu_mean"] > 0.25
    assert p_large["p_mu_pos"] > 0.90


def test_pattern_suspended_only_with_enough_evidence(tmp_path) -> None:
    """K7: Pattern key is suspended (TTL 7d) only when n >= 12 and LCB < 0."""
    from engine.learning_v3 import LearningEngineV3

    db = DatabaseManager(db_path=str(tmp_path / "pat.db"))
    try:
        le = LearningEngineV3(db=db)
        key = ("trend_pullback", "long", "trending_up", "rsi_30_45", "adx_20_35", "h_12_16")
        # 5 losing trades (< 12) -> NOT suspended
        assert le.evaluate_pattern_suspension(key, [-1.0] * 5) is False
        # 15 losing trades (>= 12, LCB < 0) -> suspended for 7 days
        assert le.evaluate_pattern_suspension(key, [-0.8] * 15) is True
        assert le.is_pattern_suspended(key) is True
    finally:
        db.close()


def test_calibration_not_used_until_n100() -> None:
    """K7: signal_score calibration is disabled until n >= 100 samples."""
    from engine.learning_v3 import LearningEngineV3

    le = LearningEngineV3()
    assert le.is_calibration_ready([(0.8, 1.0)] * 50) is False
    assert le.is_calibration_ready([(0.8, 1.0)] * 100) is True


def test_challenger_promotes_only_with_evidence() -> None:
    """K7: Challenger promotes over champion only when n >= 30 and statistically superior."""
    from engine.learning_v3 import LearningEngineV3

    le = LearningEngineV3()
    assert le.should_promote_challenger([0.5] * 10, [0.0] * 10) is False  # n < 30
    assert le.should_promote_challenger([0.05] * 40, [0.10] * 40) is False  # worse
    assert le.should_promote_challenger([0.35] * 40, [0.05] * 40) is True


def test_features_recorded_for_every_signal(tmp_path) -> None:
    """K7: Trade/signal features are persisted in trade_features for every signal."""
    from engine.learning_v3 import LearningEngineV3

    db = DatabaseManager(db_path=str(tmp_path / "feat.db"))
    try:
        le = LearningEngineV3(db=db)
        le.record_signal_features(
            trade_id="SIG_1",
            symbol="BTC/USDT",
            side="long",
            regime="trending_up",
            features={"strategy_id": "trend_pullback", "adx": 28.0, "rsi": 42.0, "cost_r": 0.06, "signal_score": 0.72},
        )
        row = db._conn.execute("SELECT position_id, strategy_id, regime FROM trade_features WHERE position_id = 'SIG_1'").fetchone()
        assert row is not None
        assert row[1] == "trend_pullback"
    finally:
        db.close()


# ---------------------------------------------------------------------------
# K8: LearningBudget, G1 Gate Enforcement & Thompson Bandit Tests
# ---------------------------------------------------------------------------

def test_strategy_without_g1_never_goes_live(tmp_path) -> None:
    """K8: Strategy without G1 pass cannot transition from shadow to learn (live)."""
    from engine.learning_budget import LearningBudgetManager

    db = DatabaseManager(db_path=str(tmp_path / "g1_enf.db"))
    try:
        mgr = LearningBudgetManager(db=db)
        assert mgr.can_enter_live_learn("trend_pullback", g1_passed=False) is False
        assert mgr.transition_strategy("trend_pullback", "learn", g1_passed=False) is False
        assert mgr.get_strategy_state("trend_pullback") == "shadow"

        assert mgr.transition_strategy("trend_pullback", "learn", g1_passed=True) is True
        assert mgr.get_strategy_state("trend_pullback") == "learn"
    finally:
        db.close()


def test_learning_budget_halts_live_learn_and_requires_approval(tmp_path) -> None:
    """K8: Exceeding LEARNING_BUDGET_PCT (15%) rolling 14d loss halts live learn until /onayla_ogrenme."""
    from engine.learning_budget import LearningBudgetManager

    cfg = Settings(LEARNING_BUDGET_PCT=15.0, DB_PATH=str(tmp_path / "budget.db"))
    db = DatabaseManager(db_path=cfg.DB_PATH)
    try:
        mgr = LearningBudgetManager(db=db, custom_settings=cfg)
        mgr.set_starting_equity(100.0)
        # 10 USD loss (< 15% of 100) -> allowed
        mgr.record_live_learn_pnl(-10.0)
        assert mgr.is_budget_exhausted() is False

        # Additional 6 USD loss (total -16 USD >= 15%) -> exhausted!
        mgr.record_live_learn_pnl(-6.0)
        assert mgr.is_budget_exhausted() is True
        assert mgr.can_enter_live_learn("trend_pullback", g1_passed=True) is False

        # Operator approval via /onayla_ogrenme resets budget window
        mgr.approve_budget_renewal(current_equity=84.0)
        assert mgr.is_budget_exhausted() is False
        assert mgr.can_enter_live_learn("trend_pullback", g1_passed=True) is True
    finally:
        db.close()


def test_thompson_prefers_uncertain_promising() -> None:
    """K8: Thompson sampling allocates trials to promising/uncertain strategies over clearly bad ones."""
    from engine.learning_budget import LearningBudgetManager

    mgr = LearningBudgetManager()
    posteriors = {
        "promising_uncertain": {"mu_mean": 0.15, "se": 0.20},
        "clearly_bad": {"mu_mean": -0.50, "se": 0.05},
    }
    counts = {"promising_uncertain": 0, "clearly_bad": 0}
    for s in range(50):
        pick = mgr.select_thompson_strategy(posteriors, seed=s)
        counts[pick] += 1
    assert counts["promising_uncertain"] > counts["clearly_bad"] * 3


# ---------------------------------------------------------------------------
# K9: Autonomy v2 State Machine & State Log Tests
# ---------------------------------------------------------------------------

def test_drawdown_triggers_caution_and_cooldown_and_logs(tmp_path) -> None:
    """K9: DD>=6% -> CAUTION, DD>=12% -> COOLDOWN (24h), daily loss>=4% -> HALT_DAY, all logged."""
    from engine.autonomy_v2 import AutonomyManagerV2

    cfg = Settings(DAILY_MAX_LOSS_PCT=4.0, MAX_DRAWDOWN_PCT=15.0, DB_PATH=str(tmp_path / "auto_v2.db"))
    db = DatabaseManager(db_path=cfg.DB_PATH)
    try:
        am = AutonomyManagerV2(db=db, custom_settings=cfg)
        assert am.evaluate_account_state(drawdown_pct=2.0, daily_loss_pct=0.5) == "NORMAL"
        assert am.get_risk_multiplier() == 1.0

        assert am.evaluate_account_state(drawdown_pct=7.0, daily_loss_pct=0.5) == "CAUTION"
        assert am.get_risk_multiplier() == pytest.approx(0.5)

        assert am.evaluate_account_state(drawdown_pct=13.0, daily_loss_pct=0.5) == "COOLDOWN"
        assert am.get_risk_multiplier() == 0.0

        logs = db._conn.execute("SELECT old_state, new_state, reason FROM state_log ORDER BY ts_ms ASC").fetchall()
        assert len(logs) >= 2
        assert logs[-1][1] == "COOLDOWN"
    finally:
        db.close()
