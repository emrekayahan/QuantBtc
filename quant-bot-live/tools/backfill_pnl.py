"""K0-7: One-shot operator tool to backfill real closed PnL from Binance fetch_my_trades."""
from __future__ import annotations

import argparse
import asyncio
import logging

from config.settings import settings
from database.db_manager import DatabaseManager
from engine.exchange import BinanceExchangeWrapper

logger = logging.getLogger("tools.backfill_pnl")


async def backfill_estimated_positions(db_path: str = "", dry_run: bool = True) -> int:
    db = DatabaseManager(db_path=db_path or settings.effective_db_path)
    exchange = BinanceExchangeWrapper(db=db)
    updated = 0
    try:
        rows = db._conn.execute(
            """
            SELECT position_id, symbol, side, entry_price, quantity, opened_at_ms, market_type
            FROM positions
            WHERE status != 'OPEN' AND COALESCE(pnl_estimated, FALSE) = TRUE
            """
        ).fetchall()
        for pos_id, symbol, side, entry_px, qty, opened_ms, market_type in rows:
            try:
                if str(market_type or "SPOT").upper() == "FUTURES" and exchange.futures_client is not None:
                    fut_sym = symbol if ":" in symbol else f"{symbol}:USDT"
                    trades = await exchange.futures_client.fetch_my_trades(fut_sym, since=int(opened_ms), limit=20)
                else:
                    trades = await exchange.client.fetch_my_trades(symbol, since=int(opened_ms), limit=20)
                exit_side = "sell" if str(side).lower() == "long" else "buy"
                closing = [t for t in (trades or []) if str(t.get("side", "")).lower() == exit_side]
                if not closing:
                    continue
                total_qty = sum(float(t.get("amount") or 0.0) for t in closing)
                total_quote = sum(float(t.get("cost") or (float(t.get("price") or 0.0) * float(t.get("amount") or 0.0))) for t in closing)
                if total_qty <= 0:
                    continue
                avg_exit = total_quote / total_qty
                fees = sum(float((t.get("fee") or {}).get("cost") or 0.0) for t in closing)
                pnl = (avg_exit - float(entry_px)) * float(qty) if str(side).lower() == "long" else (float(entry_px) - avg_exit) * float(qty)
                pnl -= fees
                logger.info("Backfill %s %s: exit=%.6f pnl=%.4f (dry_run=%s)", pos_id, symbol, avg_exit, pnl, dry_run)
                if not dry_run:
                    with db._lock:
                        db._conn.execute(
                            "UPDATE positions SET exit_price = ?, realized_pnl_usd = ?, pnl_estimated = FALSE WHERE position_id = ?",
                            [avg_exit, pnl, pos_id],
                        )
                    updated += 1
            except Exception as exc:
                logger.warning("Failed backfill for %s (%s): %s", pos_id, symbol, exc)
    finally:
        await exchange.close()
        db.close()
    return updated


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill estimated closed position PnL from Binance trades.")
    parser.add_argument("--apply", action="store_true", help="Write updates to DB (default is dry-run).")
    args = parser.parse_args()
    count = asyncio.run(backfill_estimated_positions(dry_run=not args.apply))
    print(f"Backfilled {count} position(s) (apply={args.apply}).")


if __name__ == "__main__":
    main()
