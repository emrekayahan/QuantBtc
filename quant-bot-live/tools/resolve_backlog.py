"""CLI tool to backfill and resolve past pending signals against exchange OHLCV candles."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from typing import Optional

from config.settings import get_settings
from database.db_manager import DatabaseManager
from engine.exchange import BinanceExchangeWrapper
from engine.outcome_resolver import OutcomeResolver

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("tools.resolve_backlog")


def parse_since(since_str: str) -> int:
    """Parse string like '7d', '24h', '30m' into timestamp ms."""
    now_ms = int(time.time() * 1000)
    s = since_str.strip().lower()
    if s.endswith("d"):
        days = float(s[:-1])
        return int(now_ms - days * 86400 * 1000)
    elif s.endswith("h"):
        hours = float(s[:-1])
        return int(now_ms - hours * 3600 * 1000)
    elif s.endswith("m"):
        mins = float(s[:-1])
        return int(now_ms - mins * 60 * 1000)
    return int(s) if s.isdigit() else int(now_ms - 7 * 86400 * 1000)


async def run_backlog_resolver(since_str: str = "7d", batch_size: int = 50) -> int:
    since_ms = parse_since(since_str)
    cfg = get_settings()
    db = DatabaseManager()
    ex = BinanceExchangeWrapper(db=db, custom_settings=cfg)
    resolver = OutcomeResolver(db=db, exchange=ex, custom_settings=cfg)

    total_resolved = 0
    try:
        logger.info("Starting signal resolution backlog since timestamp %d (%s)...", since_ms, since_str)
        while True:
            # Query unresolved decisions since since_ms
            query = f"""
                SELECT r.* FROM reflex_decisions r
                LEFT JOIN signal_outcomes s ON r.id = s.decision_id
                WHERE s.decision_id IS NULL AND r.setup_quality >= 1 AND r.causal_timestamp_ms >= {since_ms}
                ORDER BY r.causal_timestamp_ms ASC LIMIT {batch_size}
            """
            with db._lock:
                cursor = db._conn.execute(query)
                cols = [d[0] for d in cursor.description]
                decisions = [dict(zip(cols, r)) for r in cursor.fetchall()]

            if not decisions:
                break

            resolved_batch = await resolver.resolve_pending_signals()
            total_resolved += resolved_batch
            logger.info("Batch resolved: %d signals (total: %d)", resolved_batch, total_resolved)
            if resolved_batch == 0:
                break
    finally:
        await ex.close()
        db.close()

    logger.info("Backfill complete! Total signals resolved: %d", total_resolved)
    return total_resolved


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Resolve unresolved reflex signals from past candles.")
    parser.add_argument("--since", default="7d", help="Lookback window (e.g. '7d', '24h', '30m')")
    parser.add_argument("--limit", type=int, default=50, help="Batch limit per iteration")
    args = parser.parse_args()

    asyncio.run(run_backlog_resolver(since_str=args.since, batch_size=args.limit))
