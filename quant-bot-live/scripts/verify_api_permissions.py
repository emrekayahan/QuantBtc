"""
Verify Binance API Read Permissions
Safely runs a read-only fetch_my_trades call using credentials from .env.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import ccxt
from dotenv import load_dotenv

env_file = os.getenv("ENV_FILE")
if env_file and Path(env_file).exists():
    env_path = Path(env_file)
elif (Path(__file__).resolve().parent.parent / ".env").exists():
    env_path = Path(__file__).resolve().parent.parent / ".env"
elif Path("/home/emrekayahan/btc/quant-bot-live/.env").exists():
    env_path = Path("/home/emrekayahan/btc/quant-bot-live/.env")
else:
    env_path = Path(".env")
load_dotenv(env_path)

key = os.getenv("BINANCE_API_KEY", "").strip()
secret = os.getenv("BINANCE_API_SECRET", "").strip()

if not key or not secret or key.startswith("your_"):
    print("ERROR: Valid BINANCE_API_KEY and BINANCE_API_SECRET not found in .env")
    sys.exit(1)

client = ccxt.binance({
    "apiKey": key,
    "secret": secret,
    "enableRateLimit": True,
})

try:
    trades = client.fetch_my_trades("BTC/USDT", limit=3)
    print(f"VERIFICATION SUCCESS: Binance trade history reading is active. Retrieved {len(trades)} recent trades:")
    for t in trades:
        print(f"  • ID: {t.get('id')} | Side: {t.get('side')} | Amount: {t.get('amount')} {t.get('symbol')} | Price: ${float(t.get('price', 0)):.2f} | Time: {t.get('datetime')}")
    sys.exit(0)
except Exception as exc:
    print(f"VERIFICATION FAILED: Could not fetch trade history: {exc}")
    sys.exit(1)
