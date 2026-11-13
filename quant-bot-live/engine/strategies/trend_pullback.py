"""S1: trend_pullback — 1h trend (EMA50/200 + ADX>=22) with 15m EMA21 pullback & RSI turn."""
from __future__ import annotations

from typing import Any, Dict, Optional

from config.settings import Settings, settings
from engine.exit_engine import ExitPlan
from engine.strategies.base import (
    StrategySignal,
    adx,
    atr,
    btc_allows_direction,
    ema,
    passes_cost_and_stop_gate,
    rsi,
)


def score(state: Dict[str, Any]) -> Optional[StrategySignal]:
    df_15m = state.get("df_15m")
    df_1h = state.get("df_1h")
    if df_15m is None or df_1h is None or len(df_15m) < 30 or len(df_1h) < 60:
        return None

    cfg: Settings = state.get("settings") or settings

    # 1h trend filter
    c1h = df_1h["close"].astype(float)
    ema50_1h = float(ema(c1h, 50).iloc[-1])
    ema200_1h = float(ema(c1h, min(200, len(c1h))).iloc[-1])
    adx_1h = float(adx(df_1h, 14).iloc[-1])
    if adx_1h < 22.0:
        return None

    trend_dir: Optional[str] = None
    if ema50_1h > ema200_1h and float(c1h.iloc[-1]) > ema50_1h:
        trend_dir = "long"
    elif ema50_1h < ema200_1h and float(c1h.iloc[-1]) < ema50_1h:
        trend_dir = "short"
    if trend_dir is None or not btc_allows_direction(state, trend_dir):
        return None

    # 15m pullback to EMA21 + RSI turn + volume confirmation
    c15 = df_15m["close"].astype(float)
    l15 = df_15m["low"].astype(float)
    h15 = df_15m["high"].astype(float)
    v15 = df_15m["volume"].astype(float)

    ema21 = ema(c15, 21)
    rsi14 = rsi(c15, 14)
    atr14 = atr(df_15m, 14)
    vol_sma20 = float(v15.rolling(20).mean().iloc[-1])

    px = float(c15.iloc[-1])
    e21 = float(ema21.iloc[-1])
    r_now = float(rsi14.iloc[-1])
    r_prev = float(rsi14.iloc[-2])
    a14 = float(atr14.iloc[-1])
    vol_now = float(v15.iloc[-1])

    if a14 <= 0.0 or px <= 0.0 or (vol_sma20 > 0 and vol_now < 0.8 * vol_sma20):
        return None

    min_stop_dist = px * (float(cfg.MIN_STOP_PCT) / 100.0)
    stop_dist = max(2.5 * a14, min_stop_dist)

    if trend_dir == "long":
        touched_ema = float(l15.iloc[-1]) <= e21 * 1.004 or float(l15.iloc[-2]) <= float(ema21.iloc[-2]) * 1.004
        if not (touched_ema and px > e21 and r_prev < 50.0 and r_now > r_prev):
            return None
        stop_px = px - stop_dist
        tp_px = px + 3.0 * stop_dist
        regime = "trending_up"
    else:
        touched_ema = float(h15.iloc[-1]) >= e21 * 0.996 or float(h15.iloc[-2]) >= float(ema21.iloc[-2]) * 0.996
        if not (touched_ema and px < e21 and r_prev > 50.0 and r_now < r_prev):
            return None
        stop_px = px + stop_dist
        tp_px = px - 3.0 * stop_dist
        regime = "trending_down"

    ok, cost_info = passes_cost_and_stop_gate(px, stop_px, trend_dir, state, entry_maker=True)
    if not ok:
        return None

    plan = ExitPlan(
        stop=stop_px,
        trail_type="chandelier",
        trail_mult=2.5,
        partial_tp=[(1.5, 0.5)],
        breakeven_at_R=1.0,
        time_stop_bars=24,
        tp_maker=True,
    )
    return StrategySignal(
        strategy_id="trend_pullback",
        side=trend_dir,
        entry=px,
        stop=stop_px,
        tp=tp_px,
        exit_plan=plan,
        entry_type="MAKER",
        max_hold_bars=24,
        trail_atr_mult=2.5,
        regime=regime,
        signal_score=min(1.0, adx_1h / 50.0),
        features={"adx_1h": round(adx_1h, 2), "rsi_15m": round(r_now, 2), "cost_r": cost_info["cost_r"]},
    )
