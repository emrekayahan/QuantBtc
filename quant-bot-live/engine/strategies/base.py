"""K2 Shared Strategy Base: StrategySignal, indicator helpers, regime & cost gates."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

import pandas as pd

from config.settings import Settings, settings
from engine.cost_model import CostModel
from engine.exit_engine import ExitPlan

STRATEGY_INITIAL_STATUS: Dict[str, str] = {
    "trend_pullback": "shadow",
    "vol_breakout": "shadow",
    "range_fade": "shadow",
    "bear_breakdown_short": "shadow",
    "mean_reversion_dip": "suspended",
    "intraday_bounce": "suspended",
}


@dataclass
class StrategySignal:
    strategy_id: str
    side: str  # "long" | "short"
    entry: float
    stop: float
    tp: float
    exit_plan: Optional[ExitPlan] = None
    entry_type: str = "MAKER"
    leverage: int = 3
    max_hold_bars: int = 24
    trail_atr_mult: float = 0.0
    regime: str = "unknown"
    signal_score: float = 0.0
    features: Dict[str, Any] = field(default_factory=dict)


def ema(series: pd.Series, span: int) -> pd.Series:
    return series.astype(float).ewm(span=span, adjust=False).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    close = series.astype(float)
    delta = close.diff()
    gain = delta.clip(lower=0.0).rolling(period).mean()
    loss = (-delta.clip(upper=0.0)).rolling(period).mean().replace(0.0, 1e-9)
    rs = gain / loss
    return 100.0 - (100.0 / (1.0 + rs))


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].astype(float)
    tr = pd.concat(
        [high - low, (high - close.shift(1)).abs(), (low - close.shift(1)).abs()],
        axis=1,
    ).max(axis=1)
    return tr.rolling(period).mean()


def adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].astype(float)
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
    minus_dm = down_move.where((down_move > up_move) & (down_move > 0), 0.0)
    tr = pd.concat(
        [high - low, (high - close.shift(1)).abs(), (low - close.shift(1)).abs()],
        axis=1,
    ).max(axis=1)
    atr_s = tr.rolling(period).mean().replace(0.0, 1e-9)
    plus_di = 100.0 * (plus_dm.rolling(period).mean() / atr_s)
    minus_di = 100.0 * (minus_dm.rolling(period).mean() / atr_s)
    dx = (100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0.0, 1e-9)).fillna(0.0)
    return dx.rolling(period).mean().fillna(0.0)


def bollinger_bands(series: pd.Series, period: int = 20, num_std: float = 2.0) -> Tuple[pd.Series, pd.Series, pd.Series, pd.Series]:
    close = series.astype(float)
    mid = close.rolling(period).mean()
    std = close.rolling(period).std(ddof=0).fillna(0.0)
    upper = mid + num_std * std
    lower = mid - num_std * std
    width = ((upper - lower) / mid.replace(0.0, 1e-9)).fillna(0.0)
    return upper, mid, lower, width


def btc_allows_direction(state: Dict[str, Any], side: str) -> bool:
    """Core regime filter: do not trade against BTC 1h/4h EMA200 when df_btc_1h is provided."""
    df_btc = state.get("df_btc_1h")
    if df_btc is None or len(df_btc) < 50:
        return True
    c = df_btc["close"].astype(float)
    span = min(200, len(c))
    ema200 = float(ema(c, span).iloc[-1])
    last_c = float(c.iloc[-1])
    if side == "long":
        return last_c >= ema200
    return last_c <= ema200


def passes_cost_and_stop_gate(
    entry: float,
    stop: float,
    side: str,
    state: Dict[str, Any],
    entry_maker: bool = True,
) -> Tuple[bool, Dict[str, Any]]:
    cfg: Settings = state.get("settings") or settings
    cm = CostModel(custom_settings=cfg)
    funding = float(state.get("funding_rate_8h", 0.0))
    res = cm.compute_cost_r(
        entry=entry,
        stop=stop,
        side=side,
        market_type="FUTURES",
        entry_maker=entry_maker,
        exit_taker=True,
        funding_rate_8h=funding,
        expected_hold_hours=4.0,
    )
    return bool(res["passes_cost_gate"]), res
