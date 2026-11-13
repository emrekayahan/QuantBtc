"""Phase F1 (K1 + K4) TDD tests: cost model, backtester, statistics, registry, and G1 proof gate."""
from __future__ import annotations

import math
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd
import pytest

from config.settings import Settings


# ---------------------------------------------------------------------------
# K4: CostModel & Trade Discipline Tests
# ---------------------------------------------------------------------------

def test_cost_gate_includes_funding() -> None:
    """K4: CostModel includes maker/taker fees, slippage, and 8h funding; blocks tight stops > MAX_COST_R."""
    from engine.cost_model import CostModel

    cfg = Settings(
        FUTURES_FEE_MAKER_PCT=0.02,
        FUTURES_FEE_TAKER_PCT=0.05,
        SLIPPAGE_PCT=0.04,
        MAX_COST_R=0.12,
        MIN_STOP_PCT=0.8,
    )
    cm = CostModel(custom_settings=cfg)

    # Long with positive funding (pays funding) and 1.0% stop
    # entry_fee=0.02%, exit_fee=0.05%, slip=0.04%, funding=0.03% over 8h -> total=0.14%
    # cost_R = 0.14% / 1.0% = 0.14 > 0.12 -> blocked!
    res = cm.compute_cost_r(
        entry=100.0,
        stop=99.0,
        side="long",
        market_type="FUTURES",
        entry_maker=True,
        exit_taker=True,
        funding_rate_8h=0.0003,  # 0.03% per 8h
        expected_hold_hours=8.0,
    )
    assert res["funding_pct"] == pytest.approx(0.03, rel=1e-4)
    assert res["cost_r"] == pytest.approx(0.14, rel=1e-4)
    assert res["passes_cost_gate"] is False

    # Same setup without funding: total=0.11%, cost_R=0.11 <= 0.12 -> passes!
    res_no_fund = cm.compute_cost_r(
        entry=100.0,
        stop=99.0,
        side="long",
        market_type="FUTURES",
        entry_maker=True,
        exit_taker=True,
        funding_rate_8h=0.0,
        expected_hold_hours=8.0,
    )
    assert res_no_fund["cost_r"] == pytest.approx(0.11, rel=1e-4)
    assert res_no_fund["passes_cost_gate"] is True


def test_maker_unfilled_is_recorded_as_missed() -> None:
    """K4: Unfilled maker limit order after ENTRY_MAKER_TIMEOUT_BARS is recorded as MISSED and excluded from WinRate."""
    from tools.research.backtest import BacktestEngine, StrategySignal
    from tools.signal_report import generate_signal_report
    from database.db_manager import DatabaseManager

    # Create 10 bars where bar 0 triggers signal with limit entry=98.0, but price stays 100..105
    base_ts = 1_700_000_000_000
    rows = []
    for i in range(10):
        rows.append([base_ts + i * 900_000, 101.0, 105.0, 100.0, 103.0, 1000.0])
    df_15m = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])

    def _always_signal(state: Dict[str, Any]) -> Optional[StrategySignal]:
        if len(state["df_15m"]) == 2:
            return StrategySignal(
                strategy_id="test_strat",
                side="long",
                entry=98.0,
                stop=95.0,
                tp=104.0,
                entry_type="MAKER",
                max_hold_bars=5,
            )
        return None

    bt = BacktestEngine(entry_maker_timeout_bars=2)
    result = bt.run(df_15m=df_15m, score_fn=_always_signal, symbol="BTC/USDT", seed=42)
    assert len(result.missed_signals) == 1
    assert result.missed_signals[0]["status"] == "MISSED"
    assert len(result.trades) == 0


def test_trade_cap_prioritizes_highest_expected_r() -> None:
    """K4: prioritize_signals filters expected_net_R <= 0 and picks highest expected_net_R up to cap."""
    from engine.cost_model import CostModel

    cm = CostModel()
    candidates = [
        {"symbol": "A/USDT", "expected_net_r": 0.12},
        {"symbol": "B/USDT", "expected_net_r": -0.05},  # negative -> excluded
        {"symbol": "C/USDT", "expected_net_r": 0.45},
        {"symbol": "D/USDT", "expected_net_r": 0.28},
        {"symbol": "E/USDT", "expected_net_r": 0.00},   # zero -> excluded
    ]
    picked = cm.prioritize_signals(candidates, max_trades_remaining=2)
    assert [p["symbol"] for p in picked] == ["C/USDT", "D/USDT"]


# ---------------------------------------------------------------------------
# K1: Backtester Core Rules Tests
# ---------------------------------------------------------------------------

def _make_df(bars: List[List[float]], start_ts: int = 1_700_000_000_000) -> pd.DataFrame:
    rows = [[start_ts + i * 900_000, b[0], b[1], b[2], b[3], b[4]] for i, b in enumerate(bars)]
    return pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])


def test_backtest_sl_first_same_candle() -> None:
    """K1: When both SL and TP are hit on the same candle, SL is executed first."""
    from tools.research.backtest import BacktestEngine, StrategySignal

    # Bar 0: signal generated at close=100; Bar 1: taker fills at open=100, then same candle hits low=97 (SL=98) AND high=106 (TP=104)
    df = _make_df([
        [100, 101, 99, 100, 1000],
        [100, 106, 97, 103, 1000],
        [103, 104, 102, 103, 1000],
    ])

    def _sig(state: Dict[str, Any]) -> Optional[StrategySignal]:
        if len(state["df_15m"]) == 1:
            return StrategySignal(strategy_id="s", side="long", entry=100.0, stop=98.0, tp=104.0, entry_type="TAKER")
        return None

    res = BacktestEngine().run(df_15m=df, score_fn=_sig, symbol="BTC/USDT", seed=42)
    assert len(res.trades) == 1
    assert res.trades[0]["exit_reason"] == "SL"
    assert res.trades[0]["r_gross"] == pytest.approx(-1.0, abs=1e-4)


def test_backtest_maker_fill_requires_touch() -> None:
    """K1: Maker limit entry fills only if a subsequent bar touches entry price."""
    from tools.research.backtest import BacktestEngine, StrategySignal

    # Bar 0: signal entry=99.0; Bar 1: low=99.5 (no touch); Bar 2: low=98.5 (touch -> fill!), Bar 3: hits TP=103
    df = _make_df([
        [100, 101, 99.5, 100, 1000],
        [100, 101, 99.5, 100.5, 1000],
        [100.5, 101, 98.5, 100, 1000],
        [100, 104, 99.5, 103, 1000],
    ])

    def _sig(state: Dict[str, Any]) -> Optional[StrategySignal]:
        if len(state["df_15m"]) == 1:
            return StrategySignal(strategy_id="s", side="long", entry=99.0, stop=97.0, tp=103.0, entry_type="MAKER")
        return None

    res = BacktestEngine(entry_maker_timeout_bars=3).run(df_15m=df, score_fn=_sig, symbol="BTC/USDT", seed=42)
    assert len(res.trades) == 1
    assert res.trades[0]["entry_bar"] == 2
    assert res.trades[0]["exit_reason"] == "TP"


def test_backtest_includes_funding() -> None:
    """K1: Backtest deducts (or credits) 8h funding payments over position hold duration."""
    from tools.research.backtest import BacktestEngine, StrategySignal

    # Hold for 36 bars (9 hours = crosses at least one 8h funding interval)
    bars = [[100, 100.5, 99.5, 100, 1000] for _ in range(40)]
    bars[-1] = [100, 105, 99.5, 104, 1000]  # hit TP=104 on last bar
    df = _make_df(bars, start_ts=1_700_000_000_000)

    def _sig(state: Dict[str, Any]) -> Optional[StrategySignal]:
        if len(state["df_15m"]) == 1:
            return StrategySignal(strategy_id="s", side="long", entry=100.0, stop=98.0, tp=104.0, entry_type="TAKER", max_hold_bars=50)
        return None

    res_no_fund = BacktestEngine(funding_rate_8h=0.0).run(df_15m=df, score_fn=_sig, symbol="BTC/USDT", seed=42)
    res_high_fund = BacktestEngine(funding_rate_8h=0.005).run(df_15m=df, score_fn=_sig, symbol="BTC/USDT", seed=42)
    assert res_high_fund.trades[0]["funding_cost_r"] > 0.0
    assert res_high_fund.trades[0]["r_net"] < res_no_fund.trades[0]["r_net"]


def test_liquidation_before_stop_is_detected() -> None:
    """K1: With high leverage and wide stop, liquidation price is hit before SL and recorded as LIQUIDATION."""
    from tools.research.backtest import BacktestEngine, StrategySignal

    # Entry=100, leverage=20x -> isolated liq price ~95.5; SL=90.0 (beyond liq!)
    df = _make_df([
        [100, 101, 99, 100, 1000],
        [100, 100, 94.0, 95.0, 1000],
    ])

    def _sig(state: Dict[str, Any]) -> Optional[StrategySignal]:
        if len(state["df_15m"]) == 1:
            return StrategySignal(strategy_id="s", side="long", entry=100.0, stop=90.0, tp=120.0, entry_type="TAKER", leverage=20)
        return None

    res = BacktestEngine().run(df_15m=df, score_fn=_sig, symbol="BTC/USDT", seed=42)
    assert len(res.trades) == 1
    assert res.trades[0]["exit_reason"] == "LIQUIDATION"


def test_no_lookahead() -> None:
    """K1: Mutating future candles after bar t must never alter the signal or decision at bar t."""
    from tools.research.backtest import BacktestEngine, StrategySignal

    bars_a = [[100 + i * 0.2, 101 + i * 0.2, 99 + i * 0.2, 100 + i * 0.2, 1000] for i in range(40)]
    bars_b = [list(b) for b in bars_a]
    # Drastically mutate future bars 25..39
    for i in range(25, 40):
        bars_b[i] = [500.0, 600.0, 400.0, 550.0, 99999.0]

    df_a = _make_df(bars_a)
    df_b = _make_df(bars_b)

    seen_a: List[float] = []
    seen_b: List[float] = []

    def _rec_a(state: Dict[str, Any]) -> Optional[StrategySignal]:
        if len(state["df_15m"]) <= 20:
            seen_a.append(float(state["df_15m"]["close"].iloc[-1]))
        return None

    def _rec_b(state: Dict[str, Any]) -> Optional[StrategySignal]:
        if len(state["df_15m"]) <= 20:
            seen_b.append(float(state["df_15m"]["close"].iloc[-1]))
        return None

    BacktestEngine().run(df_15m=df_a, score_fn=_rec_a, symbol="BTC/USDT", seed=42)
    BacktestEngine().run(df_15m=df_b, score_fn=_rec_b, symbol="BTC/USDT", seed=42)
    assert seen_a == seen_b and len(seen_a) == 20


# ---------------------------------------------------------------------------
# K1: Statistics, Registry & Section 6 Proof Gate G1 Tests
# ---------------------------------------------------------------------------

def test_cluster_bootstrap_ci_known_case() -> None:
    """K1: Cluster bootstrap computes deterministic mean, 90% CI, and 80% LCB on known clustered R-values."""
    from tools.research.stats import cluster_bootstrap_ci

    trades = []
    # 50 clusters (days), alternating 0.10 R and 0.50 R -> overall mean +0.30 R with between-cluster variance
    for day in range(50):
        base = 0.10 if day % 2 == 0 else 0.50
        for r in [base - 0.05, base + 0.05]:
            trades.append({"cluster_key": f"2026-01-{(day % 28) + 1:02d}_{day}", "r_net": r})

    out1 = cluster_bootstrap_ci(trades, n_resamples=2000, seed=123)
    out2 = cluster_bootstrap_ci(trades, n_resamples=2000, seed=123)
    assert out1 == out2  # deterministic with seed
    assert out1["mean"] == pytest.approx(0.30, abs=1e-4)
    assert out1["lcb_80"] > 0.20
    assert out1["ci_low"] < out1["mean"] < out1["ci_high"]


def test_research_run_offline_reproducible(tmp_path: Path) -> None:
    """K1: Offline research runner produces identical reproducible report for same seed."""
    from tools.research.run import run_research

    r1 = run_research(strategy="baseline", days=30, seed=42, offline=True, registry_db=str(tmp_path / "exp1.db"))
    r2 = run_research(strategy="baseline", days=30, seed=42, offline=True, registry_db=str(tmp_path / "exp2.db"))
    assert r1 == r2
    assert "Research Report: `baseline`" in r1
    assert "G1 Gate:" in r1


def test_registry_counts_trials(tmp_path: Path) -> None:
    """K1: ExperimentRegistry logs every trial, increments trial_count, and enforces max 12 configs per strategy."""
    from tools.research.registry import ExperimentRegistry

    reg = ExperimentRegistry(db_path=str(tmp_path / "exp.db"), max_configs_per_strategy=12)
    try:
        for i in range(12):
            reg.log_trial(
                strategy_id="trend_pullback",
                config={"atr_mult": 1.5 + i * 0.1},
                seed=42,
                n_trades=160,
                mean_net_r=0.12,
                lcb_80=0.02,
                g1_passed=True,
                cost_breakdown={"fee": 0.05, "funding": 0.01, "slippage": 0.04},
            )
        assert reg.get_trial_count("trend_pullback") == 12
        with pytest.raises(ValueError, match="RESEARCH_MAX_CONFIGS_PER_STRATEGY"):
            reg.log_trial(
                strategy_id="trend_pullback",
                config={"atr_mult": 9.9},
                seed=42,
                n_trades=160,
                mean_net_r=0.10,
                lcb_80=0.01,
                g1_passed=False,
                cost_breakdown={},
            )
    finally:
        reg.close()


def test_g1_proof_gate_enforces_all_criteria() -> None:
    """Section 6 (G1): G1 requires n>=150, mean_net_r>0, lcb_80>=-0.05, >=3 non-negative regimes, multiple-testing check."""
    from tools.research.gates import evaluate_g1_gate

    regimes_ok = {
        "trending_up": {"n": 60, "mean_net_r": 0.18},
        "trending_down": {"n": 50, "mean_net_r": 0.10},
        "ranging": {"n": 50, "mean_net_r": 0.04},
    }
    trades_ok = [{"cluster_key": f"d{i}", "r_net": 0.12 if i % 2 == 0 else 0.10} for i in range(160)]
    res_ok = evaluate_g1_gate(trades=trades_ok, regime_stats=regimes_ok, trial_count=5)
    assert res_ok.passed is True

    # Too few trades (n=100 < 150) -> fails
    res_small = evaluate_g1_gate(trades=trades_ok[:100], regime_stats=regimes_ok, trial_count=5)
    assert res_small.passed is False
    assert any("150" in r for r in res_small.reasons)

    # Negative regime count (only 2 non-negative regimes) -> fails
    regimes_bad = {
        "trending_up": {"n": 80, "mean_net_r": 0.25},
        "trending_down": {"n": 40, "mean_net_r": -0.15},
        "ranging": {"n": 40, "mean_net_r": 0.02},
    }
    res_reg = evaluate_g1_gate(trades=trades_ok, regime_stats=regimes_bad, trial_count=5)
    assert res_reg.passed is False
