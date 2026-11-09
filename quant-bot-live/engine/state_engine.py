"""Deterministic State Engine: converts live orderbook & CLOSED candles (df.iloc[:-1]) into a causal StateSnapshot."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

try:
    import talib

    HAS_TALIB = True
except Exception:
    HAS_TALIB = False

from database.db_manager import DatabaseManager
from engine.exchange import BinanceExchangeWrapper
from engine.scanner import compute_adx, compute_atr, compute_ema
from engine.schemas import RegimeLiteral, StateSnapshotSchema, ToxicFlowLiteral


def compute_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    if HAS_TALIB and len(close) > period:
        arr = talib.RSI(close.to_numpy(dtype=float), timeperiod=period)
        return pd.Series(arr, index=close.index).fillna(50.0)
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False).mean().replace(0.0, 1e-9)
    rs = avg_gain / avg_loss
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return rsi.fillna(50.0).clip(0.0, 100.0)


def compute_bollinger_bands(
    close: pd.Series, period: int = 20, num_std: float = 2.0
) -> tuple[pd.Series, pd.Series, pd.Series]:
    if HAS_TALIB and len(close) >= period:
        upper, mid, lower = talib.BBANDS(
            close.to_numpy(dtype=float),
            timeperiod=period,
            nbdevup=num_std,
            nbdevdn=num_std,
            matype=0,
        )
        return (
            pd.Series(upper, index=close.index).bfill(),
            pd.Series(mid, index=close.index).bfill(),
            pd.Series(lower, index=close.index).bfill(),
        )
    mid = close.rolling(window=period, min_periods=1).mean()
    std = close.rolling(window=period, min_periods=1).std(ddof=0).fillna(0.0)
    upper = mid + num_std * std
    lower = mid - num_std * std
    return upper, mid, lower


class DeterministicStateEngine:
    """Builds strictly causal numeric snapshots from closed OHLCV bars and top-10 orderbook depth."""

    def __init__(
        self,
        exchange: Optional[BinanceExchangeWrapper] = None,
        db: Optional[DatabaseManager] = None,
    ) -> None:
        self.exchange = exchange
        self.db = db

    @staticmethod
    def compute_orderbook_metrics(orderbook: Dict[str, Any], fallback_price: float) -> Dict[str, float]:
        bids = orderbook.get("bids") or []
        asks = orderbook.get("asks") or []

        best_bid = float(bids[0][0]) if bids else fallback_price * 0.9998
        best_ask = float(asks[0][0]) if asks else fallback_price * 1.0002
        if best_ask < best_bid:
            best_ask = best_bid

        mid_price = (best_bid + best_ask) / 2.0
        spread_bps = ((best_ask - best_bid) / max(mid_price, 1e-9)) * 10000.0

        bid_vol = sum(float(level[1]) for level in bids[:10])
        ask_vol = sum(float(level[1]) for level in asks[:10])
        total_vol = bid_vol + ask_vol
        if total_vol > 0.0:
            imbalance = (bid_vol - ask_vol) / total_vol
        else:
            imbalance = 0.0
        imbalance = float(np.clip(imbalance, -1.0, 1.0))

        return {
            "best_bid": best_bid,
            "best_ask": best_ask,
            "mid_price": mid_price,
            "spread_bps": max(0.0, spread_bps),
            "orderbook_imbalance": imbalance,
        }

    @staticmethod
    def detect_toxic_flow(
        spread_bps: float,
        closed_volume: float,
        vol_sma_20: float,
        orderbook_imbalance: float,
        closed_close: float,
        prev_high: float,
        ema_9: float,
        ema_21: float,
    ) -> ToxicFlowLiteral:
        # 1. Spread widening > 15.0 bps
        if spread_bps > 15.0:
            return "spread_widening"

        # 2. Volume exhaustion if current closed candle volume < 0.3 * SMA(Volume, 20) on a breakout
        is_breakout = closed_close > prev_high or closed_close > ema_9 > ema_21
        if is_breakout and vol_sma_20 > 0.0 and closed_volume < 0.30 * vol_sma_20:
            return "volume_exhaustion"

        # 3. Spoofing suspected if orderbook_imbalance < -0.65 while price attempts an upward cross
        attempting_upward_cross = closed_close >= ema_21 or ema_9 >= ema_21
        if orderbook_imbalance < -0.65 and attempting_upward_cross:
            return "spoofing_suspected"

        return "none"

    def build_snapshot_from_raw(
        self,
        symbol: str,
        ohlcv_raw: List[List[Any]],
        orderbook: Dict[str, Any],
        regime: RegimeLiteral = "trending",
        open_positions_count: int = 0,
        current_drawdown_pct: float = 0.0,
    ) -> StateSnapshotSchema:
        """
        Convert raw OHLCV + orderbook into a strictly causal StateSnapshotSchema.
        CRITICAL: Immediately drops the last forming candle (df_closed = df.iloc[:-1]).
        """
        if len(ohlcv_raw) < 5:
            raise ValueError(f"Insufficient OHLCV bars for {symbol}: got {len(ohlcv_raw)}, need >= 5")

        df = pd.DataFrame(
            ohlcv_raw, columns=["timestamp", "open", "high", "low", "close", "volume"]
        )
        # STRICT CAUSALITY: drop the unclosed forming bar at iloc[-1]
        df_closed = df.iloc[:-1].copy().reset_index(drop=True)

        causal_timestamp_ms = int(df_closed.iloc[-1]["timestamp"])
        close = df_closed["close"].astype(float)
        high = df_closed["high"].astype(float)
        low = df_closed["low"].astype(float)
        open_s = df_closed["open"].astype(float)
        volume = df_closed["volume"].astype(float)

        closed_open = float(open_s.iloc[-1])
        closed_high = float(high.iloc[-1])
        closed_low = float(low.iloc[-1])
        closed_close = float(close.iloc[-1])
        closed_volume = float(volume.iloc[-1])
        prev_high = float(high.iloc[-2]) if len(high) >= 2 else closed_high

        vol_sma_20 = float(volume.tail(20).mean()) if len(volume) >= 1 else 0.0
        rsi_series = compute_rsi(close, 14)
        rsi_14 = float(rsi_series.iloc[-1])
        prev_rsi_14 = float(rsi_series.iloc[-2]) if len(rsi_series) >= 2 else rsi_14

        ema_9 = float(compute_ema(close, 9).iloc[-1])
        ema_21 = float(compute_ema(close, 21).iloc[-1])
        ema_50 = float(compute_ema(close, 50).iloc[-1])
        ema_200 = float(compute_ema(close, min(200, len(close))).iloc[-1])

        bb_upper_s, bb_mid_s, bb_lower_s = compute_bollinger_bands(close, 20, 2.0)
        bb_upper = float(bb_upper_s.iloc[-1])
        bb_mid = float(bb_mid_s.iloc[-1])
        bb_lower = float(max(1e-9, bb_lower_s.iloc[-1]))

        adx_14 = float(compute_adx(high, low, close, 14).iloc[-1])
        atr_14 = float(max(1e-8, compute_atr(high, low, close, 14).iloc[-1]))
        realized_vol = float(atr_14 / max(closed_close, 1e-9))

        ob_metrics = self.compute_orderbook_metrics(orderbook, fallback_price=closed_close)
        toxic_flow = self.detect_toxic_flow(
            spread_bps=ob_metrics["spread_bps"],
            closed_volume=closed_volume,
            vol_sma_20=vol_sma_20,
            orderbook_imbalance=ob_metrics["orderbook_imbalance"],
            closed_close=closed_close,
            prev_high=prev_high,
            ema_9=ema_9,
            ema_21=ema_21,
        )

        snapshot = StateSnapshotSchema(
            symbol=symbol,
            causal_timestamp_ms=causal_timestamp_ms,
            regime=regime,
            mid_price=ob_metrics["mid_price"],
            best_bid=ob_metrics["best_bid"],
            best_ask=ob_metrics["best_ask"],
            spread_bps=ob_metrics["spread_bps"],
            orderbook_imbalance=ob_metrics["orderbook_imbalance"],
            closed_open=closed_open,
            closed_high=closed_high,
            closed_low=closed_low,
            closed_close=closed_close,
            closed_volume=closed_volume,
            vol_sma_20=vol_sma_20,
            rsi_14=rsi_14,
            prev_rsi_14=prev_rsi_14,
            ema_9=ema_9,
            ema_21=ema_21,
            ema_50=ema_50,
            ema_200=ema_200,
            bb_upper=bb_upper,
            bb_mid=bb_mid,
            bb_lower=bb_lower,
            adx_14=adx_14,
            atr_14=atr_14,
            realized_vol=realized_vol,
            toxic_flow=toxic_flow,
            open_positions_count=open_positions_count,
            current_drawdown_pct=max(0.0, current_drawdown_pct),
        )

        if self.db is not None:
            self.db.save_state_snapshot(snapshot.model_dump())

        return snapshot

    async def capture_live_snapshot(
        self, symbol: str, timeframe: str = "15m", limit: int = 250
    ) -> StateSnapshotSchema:
        """Fetch live OHLCV and orderbook from Binance and build a causal StateSnapshotSchema."""
        if self.exchange is None:
            raise RuntimeError("BinanceExchangeWrapper is required for capture_live_snapshot")

        ohlcv_raw = await self.exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        orderbook = await self.exchange.fetch_order_book(symbol, limit=10)

        regime_str = "trending"
        open_count = 0
        drawdown_pct = 0.0
        if self.db is not None:
            r = self.db.get_state("macro_regime", "trending") or "trending"
            if r in ("trending", "mean_reverting", "high_vol", "crisis"):
                regime_str = r
            open_count = len(self.db.get_open_positions())
            drawdown_pct = float(self.db.get_state("current_drawdown_pct", "0.0") or 0.0)

        return self.build_snapshot_from_raw(
            symbol=symbol,
            ohlcv_raw=ohlcv_raw,
            orderbook=orderbook,
            regime=regime_str,  # type: ignore[arg-type]
            open_positions_count=open_count,
            current_drawdown_pct=drawdown_pct,
        )
