"""Autonomous Market Scanner & Macro Regime Classifier (BTC/ETH + Top 20 Liquid USDT Pairs)."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Literal, Optional

import numpy as np
import pandas as pd

try:
    import talib

    HAS_TALIB = True
except Exception:
    HAS_TALIB = False

from config.settings import Settings, settings
from database.db_manager import DatabaseManager
from engine.exchange import BinanceExchangeWrapper
from engine.openrouter_pool import OpenRouterPool

logger = logging.getLogger(__name__)

RegimeType = Literal["trending", "mean_reverting", "high_vol", "crisis"]

EXCLUDED_BASE_ASSETS = {
    "USDC",
    "FDUSD",
    "DAI",
    "TUSD",
    "USDP",
    "USDE",
    "USD1",
    "RLUSD",
    "BFUSD",
    "XUSD",
    "EURI",
    "AEUR",
    "USDT",
    "BUSD",
    "EUR",
    "TRY",
    "GBP",
    "BRL",
    "JPY",
    "AUD",
    "PAXG",
}

EXCLUDED_LEVERAGED_SUFFIXES = ("UP", "DOWN", "BULL", "BEAR", "3L", "3S", "2L", "2S")


def compute_ema(series: pd.Series, period: int) -> pd.Series:
    if HAS_TALIB and len(series) >= period:
        arr = talib.EMA(series.to_numpy(dtype=float), timeperiod=period)
        return pd.Series(arr, index=series.index).bfill()
    return series.ewm(span=period, adjust=False).mean()


def compute_atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    if HAS_TALIB and len(close) > period:
        arr = talib.ATR(
            high.to_numpy(dtype=float),
            low.to_numpy(dtype=float),
            close.to_numpy(dtype=float),
            timeperiod=period,
        )
        return pd.Series(arr, index=close.index).bfill()
    prev_close = close.shift(1)
    tr = pd.concat(
        [
            (high - low).abs(),
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1.0 / period, adjust=False).mean().bfill()


def compute_adx(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    if HAS_TALIB and len(close) > period * 2:
        arr = talib.ADX(
            high.to_numpy(dtype=float),
            low.to_numpy(dtype=float),
            close.to_numpy(dtype=float),
            timeperiod=period,
        )
        return pd.Series(arr, index=close.index).fillna(20.0)

    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    atr = compute_atr(high, low, close, period=period).replace(0.0, 1e-9)
    plus_di = 100.0 * pd.Series(plus_dm, index=close.index).ewm(alpha=1.0 / period, adjust=False).mean() / atr
    minus_di = 100.0 * pd.Series(minus_dm, index=close.index).ewm(alpha=1.0 / period, adjust=False).mean() / atr
    dx = (100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0.0, 1e-9)).fillna(0.0)
    return dx.ewm(alpha=1.0 / period, adjust=False).mean().fillna(20.0)


class AutonomousMarketScanner:
    """Layer 1 Brain Scanner: detects BTC/ETH macro regime and selects top 20 liquid USDT spot pairs."""

    def __init__(
        self,
        exchange: BinanceExchangeWrapper,
        db: DatabaseManager,
        openrouter_pool: Optional[OpenRouterPool] = None,
        custom_settings: Optional[Settings] = None,
    ) -> None:
        self.exchange = exchange
        self.db = db
        self.openrouter_pool = openrouter_pool
        self.cfg = custom_settings or settings

    @staticmethod
    def is_eligible_usdt_spot_symbol(symbol: str) -> bool:
        if not symbol.endswith("/USDT") or ":" in symbol:
            return False
        base = symbol.split("/")[0].upper()
        if base in EXCLUDED_BASE_ASSETS:
            return False
        if any(base.endswith(suffix) for suffix in EXCLUDED_LEVERAGED_SUFFIXES):
            return False
        return True

    @staticmethod
    def classify_regime_from_closed_candles(df_closed: pd.DataFrame) -> Dict[str, Any]:
        """Deterministically classify regime from strictly closed BTC/ETH candles."""
        if len(df_closed) < 30:
            return {
                "regime": "mean_reverting",
                "ema_50": 0.0,
                "ema_200": 0.0,
                "adx_14": 15.0,
                "realized_vol": 0.01,
                "drop_4c_pct": 0.0,
            }

        close = df_closed["close"].astype(float)
        high = df_closed["high"].astype(float)
        low = df_closed["low"].astype(float)

        ema_50 = float(compute_ema(close, 50).iloc[-1])
        ema_200 = float(compute_ema(close, min(200, len(close))).iloc[-1])
        adx_14 = float(compute_adx(high, low, close, 14).iloc[-1])
        atr_series = compute_atr(high, low, close, 14)
        realized_vol_series = (atr_series / close.replace(0.0, 1e-9)).fillna(0.0)

        current_close = float(close.iloc[-1])
        current_vol = float(realized_vol_series.iloc[-1])
        rolling_avg_vol = float(realized_vol_series.iloc[:-1].tail(50).mean() or current_vol or 1e-6)
        vol_85th = float(np.percentile(realized_vol_series.tail(100), 85))

        ref_close_4 = float(close.iloc[-5]) if len(close) >= 5 else float(close.iloc[0])
        drop_4c_pct = ((current_close - ref_close_4) / max(ref_close_4, 1e-9)) * 100.0

        # 1. Crisis Check: BTC 1h drop > 4% in 4 candles OR volatility spike > 3x rolling average
        if drop_4c_pct < -4.0 or (rolling_avg_vol > 0 and current_vol > 3.0 * rolling_avg_vol):
            regime: RegimeType = "crisis"
        # 2. Trending Check: Price > EMA(200) and ADX(14) >= 22
        elif current_close > ema_200 and adx_14 >= 22.0:
            regime = "trending"
        # 3. High Volatility Check: ATR volatility in top 85th percentile without crash breakdown
        elif current_vol >= vol_85th and current_vol > 1.25 * rolling_avg_vol:
            regime = "high_vol"
        # 4. Mean Reverting: ADX(14) < 22 and oscillating
        else:
            regime = "mean_reverting"

        return {
            "regime": regime,
            "close": current_close,
            "ema_50": ema_50,
            "ema_200": ema_200,
            "adx_14": adx_14,
            "realized_vol": current_vol,
            "drop_4c_pct": drop_4c_pct,
        }

    async def detect_macro_regime(self) -> Dict[str, Any]:
        """Fetch BTC/USDT and ETH/USDT 1h & 15m candles (dropping forming candle) and classify macro regime."""
        btc_1h_raw = await self.exchange.fetch_ohlcv("BTC/USDT", timeframe="1h", limit=250)
        eth_1h_raw = await self.exchange.fetch_ohlcv("ETH/USDT", timeframe="1h", limit=250)

        btc_df = pd.DataFrame(
            btc_1h_raw, columns=["timestamp", "open", "high", "low", "close", "volume"]
        ).iloc[:-1]
        eth_df = pd.DataFrame(
            eth_1h_raw, columns=["timestamp", "open", "high", "low", "close", "volume"]
        ).iloc[:-1]

        btc_metrics = self.classify_regime_from_closed_candles(btc_df)
        eth_metrics = self.classify_regime_from_closed_candles(eth_df)

        # Crisis on either BTC or ETH triggers macro crisis protection
        if btc_metrics["regime"] == "crisis" or eth_metrics["regime"] == "crisis":
            final_regime: RegimeType = "crisis"
        elif btc_metrics["regime"] == "trending" and eth_metrics["regime"] in ("trending", "mean_reverting"):
            final_regime = "trending"
        elif btc_metrics["regime"] == "high_vol" or eth_metrics["regime"] == "high_vol":
            final_regime = "high_vol"
        else:
            final_regime = btc_metrics["regime"]

        deterministic_summary = (
            f"Macro Regime: {final_regime.upper()} | "
            f"BTC Close={btc_metrics.get('close', 0.0):.2f} EMA200={btc_metrics['ema_200']:.2f} "
            f"ADX={btc_metrics['adx_14']:.1f} 4hChange={btc_metrics['drop_4c_pct']:.2f}% | "
            f"ETH Close={eth_metrics.get('close', 0.0):.2f} ADX={eth_metrics['adx_14']:.1f}"
        )

        commentary = deterministic_summary
        if self.openrouter_pool is not None:
            prompt = (
                f"Evaluate this deterministic macro state in 2 sentences:\n{deterministic_summary}\n"
                "Confirm regime implications for spot long entries."
            )
            res = await self.openrouter_pool.complete_with_fallback(
                prompt=prompt,
                deterministic_fallback=deterministic_summary,
                max_tokens=450,
            )
            commentary = res["content"]

        self.db.set_state("macro_regime", final_regime)
        self.db.set_state("macro_commentary", commentary)

        return {
            "regime": final_regime,
            "btc": btc_metrics,
            "eth": eth_metrics,
            "commentary": commentary,
        }

    def filter_and_rank_tickers(self, tickers: Dict[str, Any], regime: str = "mean_reverting") -> List[Dict[str, Any]]:
        """Filter tickers by $50M 24h volume, <= 0.15% spread, hard exclusions, and rank top 20."""
        candidates: List[Dict[str, Any]] = []
        min_vol = float(self.cfg.MIN_24H_VOLUME_USD)
        max_spread_pct = float(self.cfg.MAX_SPREAD_PCT)

        for symbol, t in tickers.items():
            if not self.is_eligible_usdt_spot_symbol(symbol):
                continue

            quote_vol = float(t.get("quoteVolume") or 0.0)
            if quote_vol < min_vol:
                continue

            bid = float(t.get("bid") or 0.0)
            ask = float(t.get("ask") or 0.0)
            last = float(t.get("last") or t.get("close") or 0.0)
            if bid <= 0.0 or ask <= 0.0 or ask < bid:
                continue

            mid_price = (bid + ask) / 2.0
            spread_pct = ((ask - bid) / mid_price) * 100.0
            if spread_pct > max_spread_pct:
                continue

            spread_bps = spread_pct * 100.0
            change_24h_pct = float(t.get("percentage") or 0.0)
            # Volume momentum score favors high liquidity with positive momentum
            momentum_multiplier = 1.0 + max(-0.5, min(0.5, change_24h_pct / 100.0))
            score = quote_vol * momentum_multiplier

            candidates.append(
                {
                    "symbol": symbol,
                    "volume_24h_usd": quote_vol,
                    "spread_bps": round(spread_bps, 2),
                    "price": last if last > 0.0 else mid_price,
                    "change_24h_pct": round(change_24h_pct, 2),
                    "regime": regime,
                    "_score": score,
                }
            )

        candidates.sort(key=lambda x: x["_score"], reverse=True)
        top_n = candidates[: int(self.cfg.TOP_WATCHLIST_SIZE)]
        for idx, item in enumerate(top_n, start=1):
            item["rank"] = idx
            item.pop("_score", None)
        return top_n

    async def run_scan_cycle(self) -> Dict[str, Any]:
        """Execute full 1-hour Brain scan: detect macro regime + update active_watchlist in DuckDB."""
        macro = await self.detect_macro_regime()
        regime = macro["regime"]
        tickers = await self.exchange.fetch_tickers()
        watchlist = self.filter_and_rank_tickers(tickers, regime=regime)
        self.db.upsert_watchlist(watchlist)
        logger.info(
            "Scanner cycle complete: Regime=%s, Approved Watchlist=%d symbols",
            regime,
            len(watchlist),
        )
        return {
            "regime": regime,
            "watchlist_count": len(watchlist),
            "watchlist": watchlist,
            "commentary": macro["commentary"],
        }
