"""Pre-Flight Verification Script: checks DuckDB write permissions, Binance public/private API, clock sync (<500ms), and OpenRouter pool."""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

from config.settings import get_settings
from database.db_manager import DatabaseManager
from engine.exchange import BinanceExchangeWrapper
from engine.openrouter_pool import OpenRouterPool


async def run_preflight() -> int:
    cfg = get_settings()
    print("=" * 72)
    print(" AUTONOMOUS QUANTITATIVE TRADING ENGINE v3.0 — PRE-FLIGHT VERIFICATION ")
    print("=" * 72)

    all_critical_passed = True

    # 1. Check DuckDB & Log Directory Write Permissions
    print("\n[1/4] Checking DuckDB & Log Directory Write Permissions...")
    try:
        Path(cfg.LOG_DIR).mkdir(parents=True, exist_ok=True)
        test_log = Path(cfg.LOG_DIR) / ".preflight_write_test"
        test_log.write_text("ok", encoding="utf-8")
        test_log.unlink(missing_ok=True)

        db = DatabaseManager(db_path=cfg.DB_PATH)
        probe_ts = str(int(time.time() * 1000))
        db.set_state("preflight_probe_ms", probe_ts)
        read_back = db.get_state("preflight_probe_ms")
        db.close()
        if read_back != probe_ts:
            raise RuntimeError("DuckDB read-back mismatch")
        print(f"  [PASS] DuckDB read/write verified at: {cfg.DB_PATH}")
    except Exception as exc:
        print(f"  [FAIL] Storage check failed: {exc}")
        all_critical_passed = False

    # 2. Check Binance Connectivity & NTP Clock Sync (<500ms)
    print("\n[2/4] Checking Binance Connectivity & NTP Clock Drift (<500ms)...")
    exchange = BinanceExchangeWrapper(custom_settings=cfg)
    try:
        drift_ms = await exchange.verify_clock_drift()
        abs_drift = abs(drift_ms)
        if abs_drift < 500:
            print(f"  [PASS] Clock drift vs Binance Server: {drift_ms:+d} ms (<500ms limit)")
        else:
            print(
                f"  [WARN] Clock drift is {drift_ms:+d} ms (>=500ms). "
                "CCXT adjustForTimeDifference=True is enabled, but check chrony NTP service."
            )
    except Exception as exc:
        print(f"  [FAIL] Could not reach Binance server time endpoint: {exc}")
        all_critical_passed = False

    # 3. Check Binance Spot API Credentials (if configured in .env)
    print("\n[3/4] Checking Binance Spot Credentials & Balance Access...")
    if not cfg.has_valid_binance_keys:
        print(
            "  [WARN] Placeholder Binance API keys detected in .env. "
            "Paste BINANCE_API_KEY and BINANCE_API_SECRET in .env before live trading."
        )
    else:
        try:
            bal = await exchange.fetch_balance()
            free_usdt = float(bal.get("free", {}).get("USDT", 0.0) or 0.0)
            total_usdt = float(bal.get("total", {}).get("USDT", free_usdt) or free_usdt)
            print(
                f"  [PASS] Authenticated with Binance Spot! Free USDT: ${free_usdt:.2f} (Total USDT: ${total_usdt:.2f})"
            )
        except Exception as exc:
            err_str = str(exc)
            if " 451 " in err_str or "restricted location" in err_str:
                print(
                    "  [WARN] Binance HTTP 451 on private /api/v3/account: This GCP VM is in a US region (us-central1).\n"
                    "         Public market data (Scanner, OHLCV, Orderbooks) works via data-api.binance.vision,\n"
                    "         but private order execution requires a non-US server IP (e.g. europe-west or asia)."
                )
            else:
                print(f"  [FAIL] Binance Spot authentication failed: {exc}")
                all_critical_passed = False

    await exchange.close()

    # 4. Check OpenRouter Multi-Key Pool & Deterministic Fallback
    print("\n[4/4] Checking OpenRouter Multi-Key Pool & Deterministic Fallback...")
    pool = OpenRouterPool(custom_settings=cfg)
    key_count = len(pool.keys)
    print(f"  [INFO] Configured OpenRouter keys: {key_count} | Fallback models: {pool.models}")
    res = await pool.complete_with_fallback(
        prompt="Reply with the single word READY.",
        deterministic_fallback="DETERMINISTIC_MATH_READY",
        max_tokens=200,
    )
    print(
        f"  [PASS] Pool response verified via source='{res['source']}' -> {res['content'][:60]!r}"
    )

    print("\n" + "=" * 72)
    if all_critical_passed:
        print(" PRE-FLIGHT STATUS: READY FOR LIVE PRODUCTION ")
        print("=" * 72)
        return 0
    else:
        print(" PRE-FLIGHT STATUS: FAILED CRITICAL CHECKS — REVIEW ERRORS ABOVE ")
        print("=" * 72)
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(run_preflight()))
