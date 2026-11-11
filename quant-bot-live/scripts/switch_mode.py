"""
Mode Switcher CLI: scripts/switch_mode.py paper|live
Safely validates that no active open positions or pending orders exist
before changing TRADING_MODE in .env, preventing orphan liquidations.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from pathlib import Path

from config.settings import settings
from database.db_manager import DatabaseManager


async def switch_mode(target_mode: str) -> None:
    target_mode = target_mode.lower().strip()
    if target_mode not in ("paper", "live"):
        print(f"ERROR: Invalid mode '{target_mode}'. Allowed modes: paper, live")
        sys.exit(1)

    current_mode = str(settings.TRADING_MODE).lower()
    if target_mode == current_mode:
        print(f"Bot is already configured for '{target_mode}' mode.")
        return

    print(f"\n[Mode Switch] Requested switch from '{current_mode}' to '{target_mode}'")
    print(f"[Mode Switch] Active database: {settings.effective_db_path}")

    # 1. Verify DB open positions
    db = DatabaseManager()
    open_positions = db.get_open_positions(include_unprotected=True)
    open_orders = db.get_open_orders()
    db.close()

    if open_positions or open_orders:
        print("\n❌ CANNOT SWITCH MODE: Active positions or orders found in database:")
        if open_positions:
            print(f"  Open Positions ({len(open_positions)}):")
            for p in open_positions:
                print(f"    - ID: {p.get('position_id')} | Sym: {p.get('symbol')} | Cost: ${p.get('cost_usd', 0):.2f} | Type: {p.get('market_type')}")
        if open_orders:
            print(f"  Pending Orders ({len(open_orders)}):")
            for o in open_orders:
                print(f"    - ID: {o.get('client_order_id')} | Sym: {o.get('symbol')} | Side: {o.get('side')} | Status: {o.get('status')}")
        print("\nAll open positions and orders must be fully closed/reconciled before switching trading mode.")
        sys.exit(1)

    # 2. Update .env file
    env_file = settings.BASE_DIR / ".env"
    if not env_file.exists():
        print(f"ERROR: .env file not found at {env_file}")
        sys.exit(1)

    env_content = env_file.read_text(encoding="utf-8")
    if "TRADING_MODE=" in env_content:
        new_content = re.sub(r"TRADING_MODE\s*=\s*[a-zA-Z0-9_-]+", f"TRADING_MODE={target_mode}", env_content)
    else:
        new_content = env_content + f"\nTRADING_MODE={target_mode}\n"

    env_file.write_text(new_content, encoding="utf-8")
    print(f"✅ Successfully updated {env_file} to TRADING_MODE={target_mode}")
    print("Please restart quant-bot service when ready.\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Safely switch trading mode between paper and live")
    parser.add_argument("mode", choices=["paper", "live"], help="Target mode to switch to")
    args = parser.parse_args()

    asyncio.run(switch_mode(args.mode))


if __name__ == "__main__":
    main()
