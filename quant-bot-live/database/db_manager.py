"""Thread-safe DuckDB manager for trades, orders, states, watchlist, and Brier logs."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import duckdb

from config.settings import settings


class DatabaseManager:
    """Thread-safe DuckDB persistence engine for live trading telemetry and state."""

    def __init__(self, db_path: Optional[str] = None) -> None:
        self.db_path = db_path or settings.DB_PATH
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = duckdb.connect(self.db_path)
        self._initialize_schema()

    def _initialize_schema(self) -> None:
        with self._lock:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS system_state (
                    key VARCHAR PRIMARY KEY,
                    value VARCHAR NOT NULL,
                    updated_at_ms BIGINT NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS active_watchlist (
                    symbol VARCHAR PRIMARY KEY,
                    rank INTEGER NOT NULL,
                    volume_24h_usd DOUBLE NOT NULL,
                    spread_bps DOUBLE NOT NULL,
                    price DOUBLE NOT NULL,
                    change_24h_pct DOUBLE NOT NULL,
                    rsi_14 DOUBLE DEFAULT 50.0,
                    setup_quality INTEGER DEFAULT 0,
                    direction_confidence DOUBLE DEFAULT 0.0,
                    regime VARCHAR DEFAULT 'mean_reverting',
                    toxic_flow VARCHAR DEFAULT 'none',
                    updated_at_ms BIGINT NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS state_snapshots (
                    id VARCHAR PRIMARY KEY,
                    symbol VARCHAR NOT NULL,
                    causal_timestamp_ms BIGINT NOT NULL,
                    regime VARCHAR NOT NULL,
                    mid_price DOUBLE NOT NULL,
                    spread_bps DOUBLE NOT NULL,
                    orderbook_imbalance DOUBLE NOT NULL,
                    rsi_14 DOUBLE NOT NULL,
                    ema_9 DOUBLE NOT NULL,
                    ema_21 DOUBLE NOT NULL,
                    ema_50 DOUBLE NOT NULL,
                    ema_200 DOUBLE NOT NULL,
                    adx_14 DOUBLE NOT NULL,
                    atr_14 DOUBLE NOT NULL,
                    realized_vol DOUBLE NOT NULL,
                    toxic_flow VARCHAR NOT NULL,
                    payload_json VARCHAR NOT NULL,
                    created_at_ms BIGINT NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS reflex_decisions (
                    id VARCHAR PRIMARY KEY,
                    symbol VARCHAR NOT NULL,
                    causal_timestamp_ms BIGINT NOT NULL,
                    regime VARCHAR NOT NULL,
                    direction VARCHAR NOT NULL,
                    toxic_flow VARCHAR NOT NULL,
                    setup_quality INTEGER NOT NULL,
                    direction_confidence DOUBLE NOT NULL,
                    risk_state VARCHAR NOT NULL,
                    entry_price DOUBLE NOT NULL,
                    invalidation_price DOUBLE NOT NULL,
                    take_profit_price DOUBLE NOT NULL,
                    expected_reward_risk_ratio DOUBLE NOT NULL,
                    rationale VARCHAR NOT NULL,
                    executed BOOLEAN DEFAULT FALSE,
                    outcome INTEGER,
                    created_at_ms BIGINT NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS orders (
                    client_order_id VARCHAR PRIMARY KEY,
                    exchange_order_id VARCHAR,
                    symbol VARCHAR NOT NULL,
                    side VARCHAR NOT NULL,
                    order_type VARCHAR NOT NULL,
                    status VARCHAR NOT NULL,
                    price DOUBLE NOT NULL,
                    stop_price DOUBLE,
                    amount DOUBLE NOT NULL,
                    filled DOUBLE NOT NULL,
                    cost_usd DOUBLE NOT NULL,
                    causal_timestamp_ms BIGINT NOT NULL,
                    created_at_ms BIGINT NOT NULL,
                    updated_at_ms BIGINT NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS positions (
                    position_id VARCHAR PRIMARY KEY,
                    symbol VARCHAR NOT NULL,
                    status VARCHAR NOT NULL,
                    entry_client_order_id VARCHAR NOT NULL,
                    entry_exchange_order_id VARCHAR,
                    sl_client_order_id VARCHAR,
                    sl_exchange_order_id VARCHAR,
                    entry_price DOUBLE NOT NULL,
                    current_price DOUBLE NOT NULL,
                    invalidation_price DOUBLE NOT NULL,
                    take_profit_price DOUBLE NOT NULL,
                    quantity DOUBLE NOT NULL,
                    cost_usd DOUBLE NOT NULL,
                    unrealized_pnl_usd DOUBLE DEFAULT 0.0,
                    realized_pnl_usd DOUBLE DEFAULT 0.0,
                    direction_confidence DOUBLE DEFAULT 0.80,
                    setup_quality INTEGER DEFAULT 2,
                    regime VARCHAR DEFAULT 'trending',
                    opened_at_ms BIGINT NOT NULL,
                    closed_at_ms BIGINT,
                    close_reason VARCHAR
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS brier_calibration (
                    review_date VARCHAR PRIMARY KEY,
                    sample_count INTEGER NOT NULL,
                    brier_score DOUBLE NOT NULL,
                    min_confidence_threshold DOUBLE NOT NULL,
                    max_position_usd DOUBLE NOT NULL,
                    notes VARCHAR NOT NULL,
                    created_at_ms BIGINT NOT NULL
                )
                """
            )

            # Seed default system state keys if not present
            defaults = {
                "system_halted": "false",
                "macro_regime": "mean_reverting",
                "macro_commentary": "Deterministic baseline active.",
                "min_confidence_threshold": str(settings.MIN_CONFIDENCE_THRESHOLD),
                "max_position_usd": str(settings.MAX_POSITION_USD),
                "total_equity_usd": "0.0",
                "free_usdt": "0.0",
                "peak_equity_usd": "0.0",
                "current_drawdown_pct": "0.0",
                "daily_realized_pnl_usd": "0.0",
                "auth_error_banner": "",
                "clock_drift_ms": "0",
            }
            now_ms = int(time.time() * 1000)
            for k, v in defaults.items():
                self._conn.execute(
                    """
                    INSERT INTO system_state (key, value, updated_at_ms)
                    VALUES (?, ?, ?)
                    ON CONFLICT (key) DO NOTHING
                    """,
                    [k, v, now_ms],
                )

    def set_state(self, key: str, value: Any) -> None:
        now_ms = int(time.time() * 1000)
        str_val = str(value) if not isinstance(value, (dict, list)) else json.dumps(value)
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO system_state (key, value, updated_at_ms)
                VALUES (?, ?, ?)
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at_ms = EXCLUDED.updated_at_ms
                """,
                [key, str_val, now_ms],
            )

    def get_state(self, key: str, default: Optional[str] = None) -> Optional[str]:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM system_state WHERE key = ?", [key]
            ).fetchone()
            return row[0] if row is not None else default

    def get_all_system_state(self) -> Dict[str, str]:
        with self._lock:
            rows = self._conn.execute("SELECT key, value FROM system_state").fetchall()
            return {r[0]: r[1] for r in rows}

    def is_system_halted(self) -> bool:
        val = (self.get_state("system_halted", "false") or "false").lower()
        return val in ("true", "1", "yes")

    def set_system_halted(self, halted: bool, reason: str = "") -> None:
        self.set_state("system_halted", "true" if halted else "false")
        if reason:
            self.set_state("halt_reason", reason)

    def pause_symbol(self, symbol: str, duration_seconds: int = 14400) -> None:
        until_ms = int(time.time() * 1000) + int(duration_seconds * 1000)
        self.set_state(f"paused_symbol:{symbol}", str(until_ms))

    def is_symbol_paused(self, symbol: str) -> bool:
        val = self.get_state(f"paused_symbol:{symbol}")
        if not val:
            return False
        try:
            until_ms = int(val)
            return int(time.time() * 1000) < until_ms
        except ValueError:
            return False

    def upsert_watchlist(self, items: List[Dict[str, Any]]) -> None:
        now_ms = int(time.time() * 1000)
        with self._lock:
            self._conn.execute("DELETE FROM active_watchlist")
            for idx, item in enumerate(items, start=1):
                self._conn.execute(
                    """
                    INSERT INTO active_watchlist (
                        symbol, rank, volume_24h_usd, spread_bps, price, change_24h_pct,
                        rsi_14, setup_quality, direction_confidence, regime, toxic_flow, updated_at_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        item["symbol"],
                        int(item.get("rank", idx)),
                        float(item.get("volume_24h_usd", 0.0)),
                        float(item.get("spread_bps", 0.0)),
                        float(item.get("price", 0.0)),
                        float(item.get("change_24h_pct", 0.0)),
                        float(item.get("rsi_14", 50.0)),
                        int(item.get("setup_quality", 0)),
                        float(item.get("direction_confidence", 0.0)),
                        str(item.get("regime", "mean_reverting")),
                        str(item.get("toxic_flow", "none")),
                        now_ms,
                    ],
                )

    def update_watchlist_telemetry(
        self,
        symbol: str,
        rsi_14: float,
        setup_quality: int,
        direction_confidence: float,
        regime: str,
        toxic_flow: str,
        spread_bps: Optional[float] = None,
        price: Optional[float] = None,
    ) -> None:
        now_ms = int(time.time() * 1000)
        with self._lock:
            if spread_bps is not None and price is not None:
                self._conn.execute(
                    """
                    UPDATE active_watchlist
                    SET rsi_14 = ?, setup_quality = ?, direction_confidence = ?,
                        regime = ?, toxic_flow = ?, spread_bps = ?, price = ?, updated_at_ms = ?
                    WHERE symbol = ?
                    """,
                    [
                        float(rsi_14),
                        int(setup_quality),
                        float(direction_confidence),
                        str(regime),
                        str(toxic_flow),
                        float(spread_bps),
                        float(price),
                        now_ms,
                        symbol,
                    ],
                )
            else:
                self._conn.execute(
                    """
                    UPDATE active_watchlist
                    SET rsi_14 = ?, setup_quality = ?, direction_confidence = ?,
                        regime = ?, toxic_flow = ?, updated_at_ms = ?
                    WHERE symbol = ?
                    """,
                    [
                        float(rsi_14),
                        int(setup_quality),
                        float(direction_confidence),
                        str(regime),
                        str(toxic_flow),
                        now_ms,
                        symbol,
                    ],
                )

    def get_watchlist(self) -> List[Dict[str, Any]]:
        with self._lock:
            cursor = self._conn.execute(
                """
                SELECT symbol, rank, volume_24h_usd, spread_bps, price, change_24h_pct,
                       rsi_14, setup_quality, direction_confidence, regime, toxic_flow, updated_at_ms
                FROM active_watchlist
                ORDER BY rank ASC
                """
            )
            cols = [d[0] for d in cursor.description]
            return [dict(zip(cols, row)) for row in cursor.fetchall()]

    def save_state_snapshot(self, snapshot: Dict[str, Any]) -> None:
        now_ms = int(time.time() * 1000)
        snap_id = f"{snapshot['symbol']}_{snapshot['causal_timestamp_ms']}"
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO state_snapshots (
                    id, symbol, causal_timestamp_ms, regime, mid_price, spread_bps,
                    orderbook_imbalance, rsi_14, ema_9, ema_21, ema_50, ema_200,
                    adx_14, atr_14, realized_vol, toxic_flow, payload_json, created_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (id) DO UPDATE SET
                    mid_price = EXCLUDED.mid_price,
                    spread_bps = EXCLUDED.spread_bps,
                    orderbook_imbalance = EXCLUDED.orderbook_imbalance,
                    payload_json = EXCLUDED.payload_json,
                    created_at_ms = EXCLUDED.created_at_ms
                """,
                [
                    snap_id,
                    snapshot["symbol"],
                    int(snapshot["causal_timestamp_ms"]),
                    str(snapshot.get("regime", "mean_reverting")),
                    float(snapshot["mid_price"]),
                    float(snapshot["spread_bps"]),
                    float(snapshot["orderbook_imbalance"]),
                    float(snapshot["rsi_14"]),
                    float(snapshot["ema_9"]),
                    float(snapshot["ema_21"]),
                    float(snapshot["ema_50"]),
                    float(snapshot["ema_200"]),
                    float(snapshot["adx_14"]),
                    float(snapshot["atr_14"]),
                    float(snapshot["realized_vol"]),
                    str(snapshot["toxic_flow"]),
                    json.dumps(snapshot),
                    now_ms,
                ],
            )

    def save_reflex_decision(
        self, decision: Dict[str, Any], executed: bool = False, outcome: Optional[int] = None
    ) -> str:
        now_ms = int(time.time() * 1000)
        dec_id = f"{decision['symbol']}_{decision['causal_timestamp_ms']}"
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO reflex_decisions (
                    id, symbol, causal_timestamp_ms, regime, direction, toxic_flow,
                    setup_quality, direction_confidence, risk_state, entry_price,
                    invalidation_price, take_profit_price, expected_reward_risk_ratio,
                    rationale, executed, outcome, created_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (id) DO UPDATE SET
                    executed = EXCLUDED.executed,
                    outcome = COALESCE(EXCLUDED.outcome, reflex_decisions.outcome)
                """,
                [
                    dec_id,
                    decision["symbol"],
                    int(decision["causal_timestamp_ms"]),
                    decision["regime"],
                    decision["direction"],
                    decision["toxic_flow"],
                    int(decision["setup_quality"]),
                    float(decision["direction_confidence"]),
                    decision["risk_state"],
                    float(decision["entry_price"]),
                    float(decision["invalidation_price"]),
                    float(decision["take_profit_price"]),
                    float(decision["expected_reward_risk_ratio"]),
                    str(decision["rationale"]),
                    bool(executed),
                    outcome,
                    now_ms,
                ],
            )
        return dec_id

    def upsert_order(self, order_data: Dict[str, Any]) -> None:
        now_ms = int(time.time() * 1000)
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO orders (
                    client_order_id, exchange_order_id, symbol, side, order_type, status,
                    price, stop_price, amount, filled, cost_usd, causal_timestamp_ms,
                    created_at_ms, updated_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (client_order_id) DO UPDATE SET
                    exchange_order_id = COALESCE(EXCLUDED.exchange_order_id, orders.exchange_order_id),
                    status = EXCLUDED.status,
                    price = EXCLUDED.price,
                    stop_price = COALESCE(EXCLUDED.stop_price, orders.stop_price),
                    filled = EXCLUDED.filled,
                    cost_usd = EXCLUDED.cost_usd,
                    updated_at_ms = EXCLUDED.updated_at_ms
                """,
                [
                    str(order_data["client_order_id"]),
                    order_data.get("exchange_order_id"),
                    str(order_data["symbol"]),
                    str(order_data["side"]),
                    str(order_data["order_type"]),
                    str(order_data["status"]),
                    float(order_data.get("price", 0.0)),
                    float(order_data["stop_price"]) if order_data.get("stop_price") is not None else None,
                    float(order_data.get("amount", 0.0)),
                    float(order_data.get("filled", 0.0)),
                    float(order_data.get("cost_usd", 0.0)),
                    int(order_data.get("causal_timestamp_ms", now_ms)),
                    int(order_data.get("created_at_ms", now_ms)),
                    now_ms,
                ],
            )

    def get_order_by_client_id(self, client_order_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            cursor = self._conn.execute(
                "SELECT * FROM orders WHERE client_order_id = ?", [client_order_id]
            )
            row = cursor.fetchone()
            if not row:
                return None
            cols = [d[0] for d in cursor.description]
            return dict(zip(cols, row))

    def open_position(self, pos: Dict[str, Any]) -> None:
        now_ms = int(time.time() * 1000)
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO positions (
                    position_id, symbol, status, entry_client_order_id, entry_exchange_order_id,
                    sl_client_order_id, sl_exchange_order_id, entry_price, current_price,
                    invalidation_price, take_profit_price, quantity, cost_usd,
                    unrealized_pnl_usd, realized_pnl_usd, direction_confidence,
                    setup_quality, regime, opened_at_ms
                ) VALUES (?, ?, 'OPEN', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0.0, 0.0, ?, ?, ?, ?)
                ON CONFLICT (position_id) DO UPDATE SET
                    status = 'OPEN',
                    sl_client_order_id = COALESCE(EXCLUDED.sl_client_order_id, positions.sl_client_order_id),
                    sl_exchange_order_id = COALESCE(EXCLUDED.sl_exchange_order_id, positions.sl_exchange_order_id),
                    quantity = EXCLUDED.quantity,
                    cost_usd = EXCLUDED.cost_usd
                """,
                [
                    str(pos["position_id"]),
                    str(pos["symbol"]),
                    str(pos["entry_client_order_id"]),
                    pos.get("entry_exchange_order_id"),
                    pos.get("sl_client_order_id"),
                    pos.get("sl_exchange_order_id"),
                    float(pos["entry_price"]),
                    float(pos.get("current_price", pos["entry_price"])),
                    float(pos["invalidation_price"]),
                    float(pos["take_profit_price"]),
                    float(pos["quantity"]),
                    float(pos["cost_usd"]),
                    float(pos.get("direction_confidence", 0.80)),
                    int(pos.get("setup_quality", 2)),
                    str(pos.get("regime", "trending")),
                    int(pos.get("opened_at_ms", now_ms)),
                ],
            )

    def update_position_price(self, position_id: str, current_price: float) -> None:
        with self._lock:
            row = self._conn.execute(
                "SELECT entry_price, quantity FROM positions WHERE position_id = ? AND status = 'OPEN'",
                [position_id],
            ).fetchone()
            if not row:
                return
            entry_price, qty = float(row[0]), float(row[1])
            unrealized = (float(current_price) - entry_price) * qty
            self._conn.execute(
                """
                UPDATE positions
                SET current_price = ?, unrealized_pnl_usd = ?
                WHERE position_id = ?
                """,
                [float(current_price), float(unrealized), position_id],
            )

    def close_position(
        self,
        position_id: str,
        exit_price: float,
        close_reason: str = "STOP_LOSS_OR_MARKET",
        fee_pct: float = 0.002,
    ) -> Optional[Dict[str, Any]]:
        now_ms = int(time.time() * 1000)
        with self._lock:
            cursor = self._conn.execute(
                "SELECT * FROM positions WHERE position_id = ?", [position_id]
            )
            row = cursor.fetchone()
            if not row:
                return None
            cols = [d[0] for d in cursor.description]
            pos = dict(zip(cols, row))
            entry_price = float(pos["entry_price"])
            qty = float(pos["quantity"])
            gross_pnl = (float(exit_price) - entry_price) * qty
            fees = (entry_price * qty + float(exit_price) * qty) * (fee_pct / 2.0)
            realized_pnl = gross_pnl - fees
            outcome = 1 if realized_pnl > 0.0 else 0

            self._conn.execute(
                """
                UPDATE positions
                SET status = 'CLOSED',
                    current_price = ?,
                    unrealized_pnl_usd = 0.0,
                    realized_pnl_usd = ?,
                    closed_at_ms = ?,
                    close_reason = ?
                WHERE position_id = ?
                """,
                [float(exit_price), float(realized_pnl), now_ms, close_reason, position_id],
            )

            # Also record binary outcome on matching reflex_decisions if present
            self._conn.execute(
                """
                UPDATE reflex_decisions
                SET outcome = ?
                WHERE symbol = ? AND executed = TRUE AND outcome IS NULL
                """,
                [outcome, pos["symbol"]],
            )

            pos["status"] = "CLOSED"
            pos["realized_pnl_usd"] = realized_pnl
            pos["closed_at_ms"] = now_ms
            pos["close_reason"] = close_reason
            return pos

    def get_open_positions(self) -> List[Dict[str, Any]]:
        with self._lock:
            cursor = self._conn.execute(
                "SELECT * FROM positions WHERE status = 'OPEN' ORDER BY opened_at_ms DESC"
            )
            cols = [d[0] for d in cursor.description]
            return [dict(zip(cols, r)) for r in cursor.fetchall()]

    def get_closed_positions_since(self, since_ms: int) -> List[Dict[str, Any]]:
        with self._lock:
            cursor = self._conn.execute(
                "SELECT * FROM positions WHERE status = 'CLOSED' AND closed_at_ms >= ? ORDER BY closed_at_ms DESC",
                [int(since_ms)],
            )
            cols = [d[0] for d in cursor.description]
            return [dict(zip(cols, r)) for r in cursor.fetchall()]

    def get_daily_realized_pnl(self) -> float:
        start_of_day_ms = int((int(time.time()) // 86400) * 86400 * 1000)
        with self._lock:
            row = self._conn.execute(
                """
                SELECT COALESCE(SUM(realized_pnl_usd), 0.0)
                FROM positions
                WHERE status = 'CLOSED' AND closed_at_ms >= ?
                """,
                [start_of_day_ms],
            ).fetchone()
            return float(row[0]) if row else 0.0

    def save_brier_calibration(
        self,
        review_date: str,
        sample_count: int,
        brier_score: float,
        min_confidence_threshold: float,
        max_position_usd: float,
        notes: str,
    ) -> None:
        now_ms = int(time.time() * 1000)
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO brier_calibration (
                    review_date, sample_count, brier_score,
                    min_confidence_threshold, max_position_usd, notes, created_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (review_date) DO UPDATE SET
                    sample_count = EXCLUDED.sample_count,
                    brier_score = EXCLUDED.brier_score,
                    min_confidence_threshold = EXCLUDED.min_confidence_threshold,
                    max_position_usd = EXCLUDED.max_position_usd,
                    notes = EXCLUDED.notes,
                    created_at_ms = EXCLUDED.created_at_ms
                """,
                [
                    review_date,
                    int(sample_count),
                    float(brier_score),
                    float(min_confidence_threshold),
                    float(max_position_usd),
                    str(notes),
                    now_ms,
                ],
            )
            self.set_state("min_confidence_threshold", str(min_confidence_threshold))
            self.set_state("max_position_usd", str(max_position_usd))

    def get_latest_brier_calibration(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            cursor = self._conn.execute(
                "SELECT * FROM brier_calibration ORDER BY created_at_ms DESC LIMIT 1"
            )
            row = cursor.fetchone()
            if not row:
                return None
            cols = [d[0] for d in cursor.description]
            return dict(zip(cols, row))

    def close(self) -> None:
        with self._lock:
            self._conn.close()
