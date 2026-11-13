"""S4: bear_breakdown_short — Bearish regime short on 15m support breakdown or EMA21 rejection."""
from __future__ import annotations

from typing import Any, Dict, Optional

from config.settings import Settings, settings
from engine.exit_engine import ExitPlan
from engine.strategies.base import (
    StrategySignal,
    atr,
    btc_allows_direction,
    ema,
    passes_cost_and_stop_gate,
)


def score(state: Dict[str, Any]) -> Optional[StrategySignal]:
    df_15m = state.get("df_15m")
    df_1h = state.get("df_1h")
    if df_15m is None or df_1h is None or len(df_15m) < 25 or len(df_1h) < 60:
        return None

    cfg: Settings = state.get("settings") or settings
    c1h = df_1h["close"].astype(float)
    ema50_1h = float(ema(c1h, 50).iloc[-1])
    ema200_1h = float(ema(c1h, min(200, len(c1h))).iloc[-1])
    if not (ema50_1h < ema200_1h and float(c1h.iloc[-1]) < ema50_1h):
        return None
    if not btc_allows_direction(state, "short"):
        return None

    c15 = df_15m["close"].astype(float)
    h15 = df_15m["high"].astype(float)
    l15 = df_15m["low"].astype(float)
    ema21 = float(ema(c15, 21).iloc[-1])
    prev_low_20 = float(l15.iloc[-21:-1].min())

    px = float(c15.iloc[-1])
    a14 = float(atr(df_15m, 14).iloc[-1])
    if a14 <= 0.0 or px <= 0.0:
        return None

    is_breakdown = px < prev_low_20
    is_ema21_reject = float(h15.iloc[-1]) >= ema21 * 0.997 and px < ema21
    if not (is_breakdown or is_ema21_reject):
        return None

    stop_dist = max(2.0 * a14, px * (float(cfg.MIN_STOP_PCT) / 100.0))
    stop_px = px + stop_dist
    tp_px = px - 3.0 * stop_dist

    ok, cost_info = passes_cost_and_stop_gate(px, stop_px, "short", state, entry_maker=True)
    if not ok:
        return None

    plan = ExitPlan(
        stop=stop_px,
        trail_type="atr",
        trail_mult=2.5,
        partial_tp=[(1.5, 0.5)],
        breakeven_at_R=1.0,
        time_stop_bars=24,
        tp_maker=True,
    )
    return StrategySignal(
        strategy_id="bear_breakdown_short",
        side="short",
        entry=px,
        stop=stop_px,
        tp=tp_px,
        exit_plan=plan,
        entry_type="MAKER",
        max_hold_bars=24,
        trail_atr_mult=2.5,
        regime="trending_down",
        signal_score=0.75 if is_breakdown else 0.65,
        features={"breakdown": is_breakdown, "ema21_reject": is_ema21_reject, "cost_r": cost_info["cost_r"]},
    )
