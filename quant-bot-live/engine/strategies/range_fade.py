"""S3: range_fade — Only in ranging regime (1h ADX<18, flat BTC); 15m BB(2σ) + extreme RSI."""
from __future__ import annotations

from typing import Any, Dict, Optional

from config.settings import Settings, settings
from engine.exit_engine import ExitPlan
from engine.strategies.base import (
    StrategySignal,
    adx,
    atr,
    bollinger_bands,
    ema,
    passes_cost_and_stop_gate,
    rsi,
)


def score(state: Dict[str, Any]) -> Optional[StrategySignal]:
    df_15m = state.get("df_15m")
    df_1h = state.get("df_1h")
    if df_15m is None or df_1h is None or len(df_15m) < 25 or len(df_1h) < 30:
        return None

    cfg: Settings = state.get("settings") or settings
    adx_1h = float(adx(df_1h, 14).iloc[-1])
    if adx_1h >= 18.0:
        return None

    df_btc = state.get("df_btc_1h")
    if df_btc is not None and len(df_btc) >= 30:
        btc_ema = ema(df_btc["close"].astype(float), 50)
        slope = abs(float(btc_ema.iloc[-1]) - float(btc_ema.iloc[-5])) / max(1e-9, float(btc_ema.iloc[-5]))
        if slope > 0.005:
            return None

    c15 = df_15m["close"].astype(float)
    upper, mid, lower, _ = bollinger_bands(c15, 20, 2.0)
    r14 = float(rsi(c15, 14).iloc[-1])
    a14 = float(atr(df_15m, 14).iloc[-1])
    px = float(c15.iloc[-1])
    bb_mid = float(mid.iloc[-1])
    bb_up = float(upper.iloc[-1])
    bb_lo = float(lower.iloc[-1])
    if a14 <= 0.0 or px <= 0.0:
        return None

    side: Optional[str] = None
    if px <= bb_lo and r14 < 32.0:
        side = "long"
    elif px >= bb_up and r14 > 68.0:
        side = "short"
    if side is None:
        return None

    stop_dist = max(1.2 * a14, px * (float(cfg.MIN_STOP_PCT) / 100.0))
    stop_px = (px - stop_dist) if side == "long" else (px + stop_dist)
    tp_px = bb_mid

    ok, cost_info = passes_cost_and_stop_gate(px, stop_px, side, state, entry_maker=True)
    if not ok:
        return None

    plan = ExitPlan(
        stop=stop_px,
        trail_type="none",
        trail_mult=0.0,
        partial_tp=[],
        breakeven_at_R=None,
        time_stop_bars=12,
        tp_maker=True,
    )
    return StrategySignal(
        strategy_id="range_fade",
        side=side,
        entry=px,
        stop=stop_px,
        tp=tp_px,
        exit_plan=plan,
        entry_type="MAKER",
        max_hold_bars=12,
        trail_atr_mult=0.0,
        regime="ranging",
        signal_score=min(1.0, abs(50.0 - r14) / 30.0),
        features={"adx_1h": round(adx_1h, 2), "rsi_15m": round(r14, 2), "cost_r": cost_info["cost_r"]},
    )
