"""K1 Public Futures Data Downloader & Local Disk Cache (research_data/)."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from config.settings import settings


def get_cache_dir(base_dir: Optional[str] = None) -> Path:
    d = Path(base_dir or settings.RESEARCH_DATA_DIR)
    d.mkdir(parents=True, exist_ok=True)
    return d


def cache_file_path(symbol: str, timeframe: str, days: int, base_dir: Optional[str] = None) -> Path:
    safe_sym = symbol.replace("/", "_").replace(":", "_")
    return get_cache_dir(base_dir) / f"{safe_sym}_{timeframe}_{days}d.csv"


def load_cached_ohlcv(symbol: str, timeframe: str, days: int, base_dir: Optional[str] = None) -> Optional[pd.DataFrame]:
    p = cache_file_path(symbol, timeframe, days, base_dir)
    if p.exists():
        return pd.read_csv(p)
    return None


def generate_synthetic_ohlcv(
    days: int = 270,
    timeframe: str = "15m",
    seed: int = 42,
    start_price: float = 50000.0,
) -> pd.DataFrame:
    """Deterministic synthetic OHLCV covering trending_up, trending_down, and ranging regimes for offline tests."""
    bars_per_day = 96 if timeframe == "15m" else 24
    bar_ms = 900_000 if timeframe == "15m" else 3_600_000
    n_bars = days * bars_per_day
    rng = np.random.default_rng(seed)

    start_ts = 1_700_000_000_000
    prices = np.empty(n_bars, dtype=np.float64)
    prices[0] = start_price
    third = max(1, n_bars // 3)
    for i in range(1, n_bars):
        if i < third:
            drift = 0.00015  # uptrend
        elif i < 2 * third:
            drift = -0.00015  # downtrend
        else:
            drift = 0.0  # ranging
        shock = rng.normal(drift, 0.004)
        prices[i] = max(10.0, prices[i - 1] * (1.0 + shock))

    opens = np.roll(prices, 1)
    opens[0] = start_price
    spread = np.abs(rng.normal(0.0025, 0.001, size=n_bars)) * prices
    highs = np.maximum(opens, prices) + spread
    lows = np.maximum(1.0, np.minimum(opens, prices) - spread)
    vols = rng.uniform(500.0, 5000.0, size=n_bars)
    timestamps = [start_ts + i * bar_ms for i in range(n_bars)]

    return pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": np.round(opens, 4),
            "high": np.round(highs, 4),
            "low": np.round(lows, 4),
            "close": np.round(prices, 4),
            "volume": np.round(vols, 2),
        }
    )


async def download_futures_history(
    symbol: str = "BTC/USDT:USDT",
    timeframe: str = "15m",
    days: int = 270,
    base_dir: Optional[str] = None,
) -> pd.DataFrame:
    """Download public Binance USD-M Futures OHLCV to local cache (no API keys required)."""
    cached = load_cached_ohlcv(symbol, timeframe, days, base_dir)
    if cached is not None and not cached.empty:
        return cached

    import ccxt.async_support as ccxt

    client = ccxt.binance({"enableRateLimit": True, "options": {"defaultType": "future"}})
    try:
        bar_ms = 900_000 if timeframe == "15m" else 3_600_000
        since = int(time.time() * 1000) - days * 86_400_000
        all_rows: List[List[Any]] = []
        while True:
            batch = await client.fetch_ohlcv(symbol, timeframe=timeframe, since=since, limit=1500)
            if not batch:
                break
            all_rows.extend(batch)
            last_ts = int(batch[-1][0])
            if last_ts <= since or len(batch) < 1500:
                break
            since = last_ts + bar_ms
        df = pd.DataFrame(all_rows, columns=["timestamp", "open", "high", "low", "close", "volume"]).drop_duplicates("timestamp")
        out_path = cache_file_path(symbol, timeframe, days, base_dir)
        df.to_csv(out_path, index=False)
        return df
    finally:
        await client.close()
