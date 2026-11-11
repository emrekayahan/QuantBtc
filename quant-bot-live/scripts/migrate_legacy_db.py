"""
Migration Script: quant_bot.db -> quant_bot_live.db
Safely and idempotently migrates legacy records using DuckDB,
backfills market_type, cleanses lesson penalties, takes backups,
and prevents split-brain operation.
"""

from __future__ import annotations

import argparse
import datetime
import logging
import os
import shutil
from pathlib import Path
from typing import Any, Dict, Optional

import duckdb

from config.settings import BASE_DIR, Settings, settings
from database.db_manager import DatabaseManager

logger = logging.getLogger(__name__)


def check_startup_safety(cfg: Settings, legacy_db_path: Optional[str] = None) -> None:
    """
    Ensure the bot does not start in live mode if an unmigrated legacy database
    (quant_bot.db) exists with open positions or pending orders.
    """
    target_legacy = Path(legacy_db_path) if legacy_db_path else (BASE_DIR / "data" / "quant_bot.db")
    if not target_legacy.exists():
        return

    try:
        conn = duckdb.connect(str(target_legacy), read_only=True)
        # Check positions
        tables = [r[0] for r in conn.execute("SELECT table_name FROM information_schema.tables").fetchall()]
        open_pos = 0
        if "positions" in tables:
            res = conn.execute("SELECT count(*) FROM positions WHERE status = 'OPEN'").fetchone()
            open_pos = res[0] if res else 0

        # Check orders
        open_orders = 0
        if "orders" in tables:
            res = conn.execute("SELECT count(*) FROM orders WHERE status = 'open'").fetchone()
            open_orders = res[0] if res else 0

        conn.close()

        if open_pos > 0 or open_orders > 0:
            msg = (
                f"LEGACY_DB_DETECTED: Legacy database ({target_legacy}) contains "
                f"{open_pos} open positions and {open_orders} pending orders. "
                "Bot startup is blocked to prevent split-brain state and orphan liquidation. "
                "Run 'python scripts/migrate_legacy_db.py' to migrate data to quant_bot_live.db."
            )
            logger.critical(msg)
            raise RuntimeError(msg)
    except Exception as exc:
        if "LEGACY_DB_DETECTED" in str(exc):
            raise
        logger.warning("Could not inspect legacy database: %s", exc)


def migrate_legacy_db(
    src_path: Optional[str] = None,
    dst_path: Optional[str] = None,
    backup_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Idempotently migrate tables from src_path (legacy quant_bot.db) to dst_path (quant_bot_live.db).
    Creates pre-migration backup before modifying destination.
    """
    src = Path(src_path) if src_path else (BASE_DIR / "data" / "quant_bot.db")
    dst = Path(dst_path) if dst_path else Path(settings.effective_db_path)
    b_dir = Path(backup_dir) if backup_dir else (BASE_DIR / "data" / "backups")

    if not src.exists():
        raise FileNotFoundError(f"Source database not found: {src}")

    b_dir.mkdir(parents=True, exist_ok=True)
    dst.parent.mkdir(parents=True, exist_ok=True)

    # 1. Take backup of destination if it exists
    ts_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d_%H%M%S")
    if dst.exists() and dst.stat().st_size > 0:
        dst_backup = b_dir / f"{dst.stem}_pre_migrate_{ts_str}.db"
        shutil.copy2(dst, dst_backup)
        logger.info("Created backup of destination DB at %s", dst_backup)
    else:
        src_backup = b_dir / f"{src.stem}_source_backup_{ts_str}.db"
        shutil.copy2(src, src_backup)
        logger.info("Created backup of source DB at %s", src_backup)

    # 2. Ensure destination schema is initialized
    dst_mgr = DatabaseManager(db_path=str(dst))
    src_conn = duckdb.connect(str(src), read_only=True)

    stats: Dict[str, int] = {
        "migrated_positions": 0,
        "migrated_orders": 0,
        "migrated_lessons": 0,
        "migrated_reflex": 0,
        "migrated_brier": 0,
        "migrated_state": 0,
    }

    try:
        tables = [r[0] for r in src_conn.execute("SELECT table_name FROM information_schema.tables").fetchall()]

        # Identify futures symbols from orders
        futures_symbols = set()
        if "orders" in tables:
            orders_data = src_conn.execute("SELECT symbol, order_type, client_order_id FROM orders").fetchall()
            for sym, o_type, cid in orders_data:
                if "STOP" in str(o_type or "").upper() or "FUT" in str(cid or ""):
                    futures_symbols.add(sym)

        # 3. Migrate POSITIONS with market_type backfill
        if "positions" in tables:
            pos_cols = [r[1] for r in src_conn.execute("PRAGMA table_info('positions')").fetchall()]
            pos_rows = src_conn.execute("SELECT * FROM positions").fetchall()
            for r in pos_rows:
                p_dict = dict(zip(pos_cols, r))
                sym = p_dict.get("symbol", "")
                m_type = p_dict.get("market_type")
                if not m_type:
                    if sym in futures_symbols or "FUT" in str(p_dict.get("position_id", "")):
                        m_type = "futures"
                    elif "/" in sym and sym.split("/")[0] in ("BTC", "ETH", "SOL", "BNB"):
                        m_type = "futures" if (p_dict.get("leverage") or 1) > 1 else "spot"
                    else:
                        m_type = "spot"
                p_dict["market_type"] = m_type

                cost = float(p_dict.get("cost_usd") or 0.0)
                lev = int(p_dict.get("leverage") or 1)
                p_dict["leverage"] = max(1, lev)
                p_dict["margin_usd"] = float(p_dict.get("margin_usd") or (cost / lev if lev > 1 else cost))
                p_dict["notional_usd"] = float(p_dict.get("notional_usd") or cost)
                p_dict["adopted"] = bool(p_dict.get("adopted", False))
                p_dict["pnl_estimated"] = bool(p_dict.get("pnl_estimated", False))

                dst_mgr.open_position(p_dict)
                stats["migrated_positions"] += 1

        # 4. Migrate ORDERS
        if "orders" in tables:
            ord_cols = [r[1] for r in src_conn.execute("PRAGMA table_info('orders')").fetchall()]
            ord_rows = src_conn.execute("SELECT * FROM orders").fetchall()
            for r in ord_rows:
                o_dict = dict(zip(ord_cols, r))
                dst_mgr.upsert_order(o_dict)
                stats["migrated_orders"] += 1

        # 5. Migrate TRADE_LESSONS (skip prompt-echo and trash lessons)
        if "trade_lessons" in tables:
            lesson_cols = [r[1] for r in src_conn.execute("PRAGMA table_info('trade_lessons')").fetchall()]
            lesson_rows = src_conn.execute("SELECT * FROM trade_lessons").fetchall()
            for r in lesson_rows:
                l_dict = dict(zip(lesson_cols, r))
                lesson_text = str(l_dict.get("lesson") or "")
                # Completely exclude prompt-echo and trash lessons
                if "The user wants" in lesson_text or "1-sentence" in lesson_text or len(lesson_text.strip()) < 15:
                    logger.info("Skipping trash/prompt-echo lesson: %s", l_dict.get("lesson_id"))
                    continue

                penalty = float(l_dict.get("penalty_points") or 0.0)
                validated = bool(l_dict.get("validated", True))

                dst_mgr._conn.execute(
                    """
                    INSERT INTO trade_lessons (
                        lesson_id, trade_id, symbol, mistake_type, lesson, penalty_points, validated, created_at_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (lesson_id) DO UPDATE SET
                        lesson = EXCLUDED.lesson,
                        penalty_points = EXCLUDED.penalty_points,
                        validated = EXCLUDED.validated
                    """,
                    [
                        str(l_dict["lesson_id"]),
                        l_dict.get("trade_id"),
                        str(l_dict.get("symbol") or "UNKNOWN"),
                        str(l_dict.get("mistake_type") or "UNKNOWN"),
                        lesson_text,
                        penalty,
                        validated,
                        int(l_dict.get("created_at_ms") or 0),
                    ],
                )
                stats["migrated_lessons"] += 1

        # 6. Migrate REFLEX_DECISIONS
        if "reflex_decisions" in tables:
            rd_cols = [r[1] for r in src_conn.execute("PRAGMA table_info('reflex_decisions')").fetchall()]
            rd_rows = src_conn.execute("SELECT * FROM reflex_decisions").fetchall()
            for r in rd_rows:
                rd = dict(zip(rd_cols, r))
                col_names = ", ".join(rd.keys())
                placeholders = ", ".join(["?"] * len(rd))
                dst_mgr._conn.execute(
                    f"INSERT OR IGNORE INTO reflex_decisions ({col_names}) VALUES ({placeholders})",
                    list(rd.values()),
                )
                stats["migrated_reflex"] += 1

        # 7. Migrate BRIER_CALIBRATION
        if "brier_calibration" in tables:
            bc_cols = [r[1] for r in src_conn.execute("PRAGMA table_info('brier_calibration')").fetchall()]
            bc_rows = src_conn.execute("SELECT * FROM brier_calibration").fetchall()
            for r in bc_rows:
                bc = dict(zip(bc_cols, r))
                # Skip uncalibrated n=1 rows
                if int(bc.get("sample_count") or 0) <= 1:
                    continue
                col_names = ", ".join(bc.keys())
                placeholders = ", ".join(["?"] * len(bc))
                dst_mgr._conn.execute(
                    f"INSERT OR IGNORE INTO brier_calibration ({col_names}) VALUES ({placeholders})",
                    list(bc.values()),
                )
                stats["migrated_brier"] += 1

        # 8. Migrate SYSTEM_STATE (selective)
        if "system_state" in tables:
            st_rows = src_conn.execute("SELECT key, value FROM system_state").fetchall()
            for key, val in st_rows:
                if key in ("min_confidence_threshold",):
                    continue
                dst_mgr.set_state(key, val)
                stats["migrated_state"] += 1

    finally:
        src_conn.close()
        dst_mgr.close()

    logger.info("Migration complete: %s", stats)
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate legacy quant_bot.db to quant_bot_live.db")
    parser.add_argument("--src", help="Path to legacy DB", default=None)
    parser.add_argument("--dst", help="Path to destination DB", default=None)
    parser.add_argument("--backup-dir", help="Path to backups directory", default=None)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    res = migrate_legacy_db(src_path=args.src, dst_path=args.dst, backup_dir=args.backup_dir)
    print("\n--- Migration Report ---")
    for k, v in res.items():
        print(f"  {k}: {v}")
    print("Done.\n")


if __name__ == "__main__":
    main()
