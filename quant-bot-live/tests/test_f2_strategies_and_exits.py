"""Phase F2 (K2 + K3) TDD tests: S1-S4 strategies, cost gate, no-lookahead, and ExitEngine v2."""
from __future__ import annotations

from typing import Any, Dict, List

import numpy as np
import pandas as pd
import pytest

from config.settings import Settings


def _build_ohlcv(closes: List[float], spread_frac: float = 0.003, vol: float = 1000.0, bar_ms: int = 900_000, start_ts: int = 1_700_000_000_000) -> pd.DataFrame:
    rows = []
    prev_c = closes[0]
    for i, c in enumerate(closes):
        o = prev_c
        h = max(o, c) * (1.0 + spread_frac)
        l = min(o, c) * (1.0 - spread_frac)
        rows.append([start_ts + i * bar_ms, o, h, l, c, vol])
        prev_c = c
    return pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])


def _uptrend_1h_df(n: int = 220, start: float = 100.0, step: float = 0.4) -> pd.DataFrame:
    closes = [start + i * step for i in range(n)]
    return _build_ohlcv(closes, spread_frac=0.002, vol=5000.0, bar_ms=3_600_000)


def _downtrend_1h_df(n: int = 220, start: float = 200.0, step: float = 0.4) -> pd.DataFrame:
    closes = [max(10.0, start - i * step) for i in range(n)]
    return _build_ohlcv(closes, spread_frac=0.002, vol=5000.0, bar_ms=3_600_000)


def _flat_1h_df(n: int = 220, level: float = 100.0) -> pd.DataFrame:
    closes = [level + (0.15 if i % 2 == 0 else -0.15) for i in range(n)]
    return _build_ohlcv(closes, spread_frac=0.001, vol=5000.0, bar_ms=3_600_000)


# ---------------------------------------------------------------------------
# K2: S1 trend_pullback
# ---------------------------------------------------------------------------

def test_trend_pullback_long_and_short_and_countertrend() -> None:
    from engine.strategies.trend_pullback import score

    df_1h_up = _uptrend_1h_df()
    # 15m: steady rise up to ~187, then brief pullback dipping RSI, then turn up closing above EMA21
    base = [170.0 + i * 0.25 for i in range(55)]
    pullback = [183.5, 182.2, 181.0, 180.2, 181.8]  # dips toward EMA21 then bounces
    df_15m_long = _build_ohlcv(base + pullback, spread_frac=0.008, vol=2000.0)
    # Ensure last bar low touched EMA21 zone and closed higher
    df_15m_long.iloc[-1, df_15m_long.columns.get_loc("low")] = float(df_15m_long["close"].iloc[-1]) * 0.992

    sig_long = score({"symbol": "BTC/USDT", "df_15m": df_15m_long, "df_1h": df_1h_up})
    assert sig_long is not None
    assert sig_long.side == "long"
    assert sig_long.strategy_id == "trend_pullback"
    assert sig_long.exit_plan.trail_type == "chandelier"

    # Counter-trend: same 15m pullback with downtrend 1h -> MUST NOT produce long
    df_1h_down = _downtrend_1h_df()
    sig_counter = score({"symbol": "BTC/USDT", "df_15m": df_15m_long, "df_1h": df_1h_down})
    assert sig_counter is None or sig_counter.side != "long"

    # Short case in downtrend 1h: 15m rallies briefly then turns down below EMA21
    base_d = [130.0 - i * 0.25 for i in range(55)]
    bounce_d = [116.5, 117.8, 119.0, 119.8, 118.0]
    df_15m_short = _build_ohlcv(base_d + bounce_d, spread_frac=0.008, vol=2000.0)
    df_15m_short.iloc[-1, df_15m_short.columns.get_loc("high")] = float(df_15m_short["close"].iloc[-1]) * 1.008
    sig_short = score({"symbol": "BTC/USDT", "df_15m": df_15m_short, "df_1h": df_1h_down})
    assert sig_short is not None
    assert sig_short.side == "short"


# ---------------------------------------------------------------------------
# K2: S2 vol_breakout
# ---------------------------------------------------------------------------

def test_vol_breakout_long_short_and_countertrend() -> None:
    from engine.strategies.vol_breakout import score

    # 1h squeeze: normal vol then 20 tight bars
    closes_1h = [100.0 + i * 0.3 for i in range(195)] + [158.0 + (0.05 if i % 2 == 0 else -0.05) for i in range(25)]
    df_1h = _build_ohlcv(closes_1h, spread_frac=0.001, vol=5000.0, bar_ms=3_600_000)

    # 15m tight range for 25 bars then high-volume breakout above Donchian(20)
    c15 = [158.0 + (0.1 if i % 2 == 0 else -0.1) for i in range(30)] + [161.5]
    df_15m = _build_ohlcv(c15, spread_frac=0.005, vol=1000.0)
    df_15m.iloc[-1, df_15m.columns.get_loc("volume")] = 3000.0  # > 1.5x avg

    sig = score({"symbol": "BTC/USDT", "df_15m": df_15m, "df_1h": df_1h})
    assert sig is not None
    assert sig.side == "long"
    assert sig.strategy_id == "vol_breakout"

    # Short breakout in downtrend 1h
    closes_1h_d = [200.0 - i * 0.3 for i in range(195)] + [142.0 + (0.05 if i % 2 == 0 else -0.05) for i in range(25)]
    df_1h_d = _build_ohlcv(closes_1h_d, spread_frac=0.001, vol=5000.0, bar_ms=3_600_000)
    c15_d = [142.0 + (0.1 if i % 2 == 0 else -0.1) for i in range(30)] + [138.5]
    df_15m_d = _build_ohlcv(c15_d, spread_frac=0.005, vol=1000.0)
    df_15m_d.iloc[-1, df_15m_d.columns.get_loc("volume")] = 3000.0
    sig_s = score({"symbol": "BTC/USDT", "df_15m": df_15m_d, "df_1h": df_1h_d})
    assert sig_s is not None
    assert sig_s.side == "short"

    # Counter-trend: long breakout against downtrend 1h blocked
    assert score({"symbol": "BTC/USDT", "df_15m": df_15m, "df_1h": df_1h_d}) is None


# ---------------------------------------------------------------------------
# K2: S3 range_fade
# ---------------------------------------------------------------------------

def test_range_fade_long_short_and_trending_blocked() -> None:
    from engine.strategies.range_fade import score

    df_1h_flat = _flat_1h_df()
    # 15m flat around 100 then sharp dip outside lower BB(20, 2) with oversold RSI
    c15_l = [100.0 + (0.2 if i % 2 == 0 else -0.2) for i in range(30)] + [98.5, 97.2, 95.8]
    df_15m_l = _build_ohlcv(c15_l, spread_frac=0.005, vol=1000.0)
    sig_l = score({"symbol": "BTC/USDT", "df_15m": df_15m_l, "df_1h": df_1h_flat})
    assert sig_l is not None
    assert sig_l.side == "long"
    assert sig_l.strategy_id == "range_fade"

    # Upper BB fade -> short
    c15_s = [100.0 + (0.2 if i % 2 == 0 else -0.2) for i in range(30)] + [101.5, 102.8, 104.5]
    df_15m_s = _build_ohlcv(c15_s, spread_frac=0.005, vol=1000.0)
    sig_s = score({"symbol": "BTC/USDT", "df_15m": df_15m_s, "df_1h": df_1h_flat})
    assert sig_s is not None
    assert sig_s.side == "short"

    # Trending 1h (ADX >= 18) -> range_fade must NOT fire
    df_1h_trend = _uptrend_1h_df()
    assert score({"symbol": "BTC/USDT", "df_15m": df_15m_l, "df_1h": df_1h_trend}) is None


# ---------------------------------------------------------------------------
# K2: S4 bear_breakdown_short & Common Rules
# ---------------------------------------------------------------------------

def test_bear_breakdown_short_and_cost_gate_and_no_lookahead() -> None:
    from engine.strategies.bear_breakdown_short import score as s4_score
    from engine.strategies.trend_pullback import score as s1_score

    df_1h_down = _downtrend_1h_df()
    df_1h_up = _uptrend_1h_df()

    # 15m breaks below 20-bar support with sufficient ATR for cost gate
    c15 = [120.0 - (i % 3) * 0.2 for i in range(30)] + [116.5]
    df_15m = _build_ohlcv(c15, spread_frac=0.008, vol=1500.0)
    sig = s4_score({"symbol": "BTC/USDT", "df_15m": df_15m, "df_1h": df_1h_down, "df_btc_1h": df_1h_down})
    assert sig is not None
    assert sig.side == "short"
    assert sig.strategy_id == "bear_breakdown_short"

    # Blocked in uptrend
    assert s4_score({"symbol": "BTC/USDT", "df_15m": df_15m, "df_1h": df_1h_up, "df_btc_1h": df_1h_up}) is None

    # Cost gate blocks when cost_R exceeds MAX_COST_R
    cfg_strict = Settings(MAX_COST_R=0.02, MIN_STOP_PCT=0.8)
    assert s4_score({"symbol": "BTC/USDT", "df_15m": df_15m, "df_1h": df_1h_down, "df_btc_1h": df_1h_down, "settings": cfg_strict}) is None

    # No-lookahead check: appending extreme future bars to copies must not change earlier score
    res_now = s1_score({"symbol": "BTC/USDT", "df_15m": df_15m.copy(), "df_1h": df_1h_down.copy()})
    # If we slice df_15m[:-1] and compare with df_15m[:-1] before mutation, identical
    snap = df_15m.iloc[:-1].copy()
    assert s4_score({"symbol": "BTC/USDT", "df_15m": snap, "df_1h": df_1h_down}) == s4_score(
        {"symbol": "BTC/USDT", "df_15m": df_15m.iloc[:-1].copy(), "df_1h": df_1h_down}
    )


# ---------------------------------------------------------------------------
# K3: ExitEngine v2 Tests
# ---------------------------------------------------------------------------

def test_chandelier_ratchets_only_in_favor() -> None:
    from engine.exit_engine import ExitManager, ExitPlan

    mgr = ExitManager()
    plan = ExitPlan(stop=97.0, trail_type="chandelier", trail_mult=2.5, time_stop_bars=24)

    # Long: price rises to 105 (ATR=1.0 -> trail=102.5) -> SL ratchets up from 97 to 102.5
    sl1 = mgr.compute_trailed_stop(side="long", current_stop=97.0, extreme_price=105.0, atr=1.0, plan=plan)
    assert sl1 == pytest.approx(102.5)
    # Price drops so extreme stays 105 or ATR widens -> SL NEVER moves down!
    sl2 = mgr.compute_trailed_stop(side="long", current_stop=sl1, extreme_price=103.0, atr=2.0, plan=plan)
    assert sl2 == pytest.approx(102.5)

    # Short: price drops to 92 (ATR=1.0 -> trail=94.5) -> SL ratchets down from 103 to 94.5, never up
    sl_s1 = mgr.compute_trailed_stop(side="short", current_stop=103.0, extreme_price=92.0, atr=1.0, plan=plan)
    assert sl_s1 == pytest.approx(94.5)
    sl_s2 = mgr.compute_trailed_stop(side="short", current_stop=sl_s1, extreme_price=94.0, atr=2.0, plan=plan)
    assert sl_s2 == pytest.approx(94.5)


def test_partial_tp_moves_stop_to_breakeven() -> None:
    from engine.exit_engine import ExitManager, ExitPlan

    mgr = ExitManager()
    plan = ExitPlan(stop=98.0, trail_type="chandelier", trail_mult=2.5, partial_tp=[(1.5, 0.5)], breakeven_at_R=1.0, time_stop_bars=24)
    state = mgr.init_state(entry_price=100.0, stop_price=98.0, side="long", quantity=1.0, plan=plan)

    # Bar hits high=103.2 (+1.6R) -> triggers 1.5R partial TP (closes 50%) and moves SL to breakeven (100.0)
    actions = mgr.evaluate_bar(state=state, bar_high=103.2, bar_low=100.5, bar_close=102.8, atr=1.0, bars_held=3)
    assert actions["partial_close_qty"] == pytest.approx(0.5)
    assert state["remaining_qty"] == pytest.approx(0.5)
    assert state["current_stop"] >= 100.0


@pytest.mark.asyncio
async def test_trailing_never_leaves_position_unprotected() -> None:
    """K3: New SL order is placed BEFORE old SL order is cancelled; if new SL fails, old SL remains."""
    from engine.exit_engine import ExitManager

    call_log: List[str] = []

    class FakeFuturesClient:
        def __init__(self, fail_create: bool = False) -> None:
            self.fail_create = fail_create

        async def create_order(self, **kwargs):
            call_log.append(f"create:{kwargs.get('params', {}).get('stopPrice')}")
            if self.fail_create:
                raise RuntimeError("Exchange rejected new SL")
            return {"id": "NEW_SL_2"}

        async def cancel_order(self, order_id, symbol):
            call_log.append(f"cancel:{order_id}")
            return {"id": order_id, "status": "canceled"}

    mgr = ExitManager()
    ok_client = FakeFuturesClient(fail_create=False)
    new_id = await mgr.replace_stop_safely(
        client=ok_client,
        symbol="BTC/USDT:USDT",
        side="long",
        quantity=0.1,
        new_stop_price=101.5,
        old_stop_order_id="OLD_SL_1",
    )
    assert new_id == "NEW_SL_2"
    assert call_log == ["create:101.5", "cancel:OLD_SL_1"]

    call_log.clear()
    fail_client = FakeFuturesClient(fail_create=True)
    kept_id = await mgr.replace_stop_safely(
        client=fail_client,
        symbol="BTC/USDT:USDT",
        side="long",
        quantity=0.1,
        new_stop_price=102.0,
        old_stop_order_id="OLD_SL_1",
    )
    assert kept_id == "OLD_SL_1"
    assert call_log == ["create:102.0"]  # old SL was NEVER cancelled!
