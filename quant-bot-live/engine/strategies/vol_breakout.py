"""S2: vol_breakout — 1h BB squeeze (bottom 20%) + 15m Donchian(20) breakout + >=1.5x volume."""
from __future__ import annotations

from typing import Any, Dict, Optional

from config.settings import Settings, settings
from engine.exit_engine import ExitPlan
from engine.strategies.base import (
    StrategySignal,
    atr,
    bollinger_bands,
    btc_allows_direction,
    ema,
    passes_cost_and_stop_gate,
)


def score(state: Dict[str, Any]) -> Optional[StrategySignal]:
    df_15m = state.get("df_15m")
    df_1h = state.get("df_1h")
    if df_15m is None or df_1h is None or len(df_15m) < 25 or len(df_1h) < 50:
        return None

    cfg: Settings = state.get("settings") or settings
    c1h = df_1h["close"].astype(float)
    _, _, _, bbw = bollinger_bands(c1h, 20, 2.0)
    bbw_recent = bbw.iloc[-20:]
    if len(bbw_recent) < 20:
        return None
    bbw_now = float(bbw.iloc[-1])
    bbw_q20 = float(bbw_recent.quantile(0.20))
    if bbw_now > bbw_q20 * 1.05:
        return None

    ema50_1h = float(ema(c1h, 50).iloc[-1])
    ema200_1h = float(ema(c1h, min(200, len(c1h))).iloc[-1])

    c15 = df_15m["close"].astype(float)
    h15 = df_15m["high"].astype(float)
    l15 = df_15m["low"].astype(float)
    v15 = df_15m["volume"].astype(float)

    prev_high_20 = float(h15.iloc[-21:-1].max())
    prev_low_20 = float(l15.iloc[-21:-1].min())
    vol_avg = float(v15.iloc[-21:-1].mean())
    vol_now = float(v15.iloc[-1])
    if vol_avg <= 0.0 or vol_now < 1.5 * vol_avg:
        return None

    px = float(c15.iloc[-1])
    a14 = float(atr(df_15m, 14).iloc[-1])
    if a14 <= 0.0 or px <= 0.0:
        return None

    side: Optional[str] = None
    if px > prev_high_20 and ema50_1h >= ema200_1h:
        side = "long"
    elif px < prev_low_20 and ema50_1h <= ema200_1h:
        side = "short"
    if side is None or not btc_allows_direction(state, side):
        return None

    stop_dist = max(2.0 * a14, px * (float(cfg.MIN_STOP_PCT) / 100.0))
    stop_px = (px - stop_dist) if side == "long" else (px + stop_dist)
    tp_px = (px + 4.0 * stop_dist) if side == "long" else (px - 4.0 * stop_dist)

    ok, cost_info = passes_cost_and_stop_gate(px, stop_px, side, state, entry_maker=True)
    if not ok:
        return None

    plan = ExitPlan(
        stop=stop_px,
        trail_type="atr",
        trail_mult=3.0,
        partial_tp=[(2.0, 0.5)],
        breakeven_at_R=1.5,
        time_stop_bars=32,
        tp_maker=True,
    )
    return StrategySignal(
        strategy_id="vol_breakout",
        side=side,
        entry=px,
        stop=stop_px,
        tp=tp_px,
        exit_plan=plan,
        entry_type="MAKER",
        max_hold_bars=32,
        trail_atr_mult=3.0,
        regime="trending_up" if side == "long" else "trending_down",
        signal_score=min(1.0, vol_now / (3.0 * vol_avg)),
        features={"bbw": round(bbw_now, 5), "vol_ratio": round(vol_now / vol_avg, 2), "cost_r": cost_info["cost_r"]},
    )
