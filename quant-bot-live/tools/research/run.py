"""K1 CLI runner: python -m tools.research.run --strategy baseline --days 270 [--offline]."""
from __future__ import annotations

import argparse
import asyncio
from typing import Any, Dict, Optional

import pandas as pd

from tools.research.backtest import BacktestEngine, StrategySignal
from tools.research.download import download_futures_history, generate_synthetic_ohlcv, load_cached_ohlcv
from tools.research.gates import evaluate_g1_gate
from tools.research.registry import ExperimentRegistry
from tools.research.report import format_research_report
from tools.research.stats import cluster_bootstrap_ci


def _classify_simple_regime(df: pd.DataFrame) -> str:
    if len(df) < 50:
        return "ranging"
    close = df["close"].astype(float)
    ema50 = close.ewm(span=50, adjust=False).mean().iloc[-1]
    ema50_prev = close.ewm(span=50, adjust=False).mean().iloc[-10]
    slope = (ema50 - ema50_prev) / max(1e-9, ema50_prev)
    if slope > 0.002:
        return "trending_up"
    if slope < -0.002:
        return "trending_down"
    return "ranging"


def baseline_legacy_score(state: Dict[str, Any]) -> Optional[StrategySignal]:
    """Deterministic representation of legacy dip/bounce baseline (D7 verification)."""
    df = state["df_15m"]
    if len(df) < 55:
        return None
    close = df["close"].astype(float)
    low = df["low"].astype(float)
    high = df["high"].astype(float)

    tr = pd.concat(
        [high - low, (high - close.shift(1)).abs(), (low - close.shift(1)).abs()],
        axis=1,
    ).max(axis=1)
    atr = float(tr.rolling(14).mean().iloc[-1])
    delta = close.diff()
    gain = delta.clip(lower=0.0).rolling(14).mean()
    loss = (-delta.clip(upper=0.0)).rolling(14).mean().replace(0.0, 1e-9)
    rsi = float((100.0 - (100.0 / (1.0 + gain / loss))).iloc[-1])
    px = float(close.iloc[-1])
    if atr <= 0.0 or px <= 0.0:
        return None

    regime = _classify_simple_regime(df)
    if rsi < 32.0:
        stop_dist = max(1.5 * atr, px * 0.008)
        return StrategySignal(
            strategy_id="baseline_legacy",
            side="long",
            entry=px,
            stop=px - stop_dist,
            tp=px + 2.5 * stop_dist,
            entry_type="MAKER",
            regime=regime,
        )
    return None


def run_research(
    strategy: str = "baseline",
    symbol: str = "BTC/USDT:USDT",
    days: int = 270,
    seed: int = 42,
    offline: bool = False,
    registry_db: Optional[str] = None,
) -> str:
    df_15m = load_cached_ohlcv(symbol, "15m", days)
    if df_15m is None:
        if offline:
            df_15m = generate_synthetic_ohlcv(days=days, timeframe="15m", seed=seed)
        else:
            df_15m = asyncio.run(download_futures_history(symbol=symbol, timeframe="15m", days=days))

    bt = BacktestEngine()
    res = bt.run(df_15m=df_15m, score_fn=baseline_legacy_score, symbol=symbol, seed=seed)
    boot = cluster_bootstrap_ci(res.trades, n_resamples=2000, seed=seed)
    reg = ExperimentRegistry(db_path=registry_db)
    try:
        trial_count = reg.get_trial_count(strategy) + 1
        gate = evaluate_g1_gate(trades=res.trades, regime_stats=res.regime_stats, trial_count=trial_count, seed=seed)
        reg.log_trial(
            strategy_id=strategy,
            config={"symbol": symbol, "days": days, "mode": "offline" if offline else "public"},
            seed=seed,
            n_trades=len(res.trades),
            mean_net_r=boot["mean"],
            lcb_80=boot["lcb_80"],
            g1_passed=gate.passed,
            cost_breakdown=res.cost_breakdown,
        )
    finally:
        reg.close()

    return format_research_report(strategy_id=strategy, result=res, gate=gate, seed=seed)


def main() -> None:
    parser = argparse.ArgumentParser(description="QuantBot V4 K1 Research Runner")
    parser.add_argument("--strategy", default="baseline")
    parser.add_argument("--symbol", default="BTC/USDT:USDT")
    parser.add_argument("--days", type=int, default=270)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args()
    print(run_research(strategy=args.strategy, symbol=args.symbol, days=args.days, seed=args.seed, offline=args.offline))


if __name__ == "__main__":
    main()
