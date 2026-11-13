"""Thread-safe DuckDB manager for trades, orders, states, watchlist, and Brier logs."""

from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

import duckdb

from config.settings import settings


class DatabaseManager:
    """Thread-safe DuckDB persistence engine for live trading telemetry and state."""

    def __init__(self, db_path: Optional[str] = None) -> None:
        self.db_path = db_path or settings.effective_db_path
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
                    market_type VARCHAR,
                    router_reason VARCHAR,
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
                    close_reason VARCHAR,
                    market_type VARCHAR DEFAULT 'spot',
                    leverage INTEGER DEFAULT 1,
                    margin_usd DOUBLE DEFAULT 0.0,
                    notional_usd DOUBLE DEFAULT 0.0,
                    sl_slippage_pct DOUBLE DEFAULT 0.0,
                    is_paper BOOLEAN DEFAULT FALSE,
                    adopted BOOLEAN DEFAULT FALSE,
                    pnl_estimated BOOLEAN DEFAULT FALSE
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
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS trade_lessons (
                    id VARCHAR PRIMARY KEY,
                    symbol VARCHAR NOT NULL,
                    regime VARCHAR NOT NULL,
                    entry_rsi DOUBLE NOT NULL,
                    orderbook_imbalance DOUBLE NOT NULL,
                    pnl DOUBLE NOT NULL,
                    failure_reason VARCHAR NOT NULL,
                    confidence_penalty DOUBLE NOT NULL,
                    created_at BIGINT NOT NULL,
                    validated BOOLEAN DEFAULT TRUE
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS paper_ledger (
                    id VARCHAR PRIMARY KEY,
                    ts_ms BIGINT NOT NULL,
                    event VARCHAR NOT NULL,
                    symbol VARCHAR,
                    amount DOUBLE NOT NULL,
                    balance_after DOUBLE NOT NULL,
                    details_json VARCHAR
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS dust_ignored (
                    symbol VARCHAR PRIMARY KEY,
                    value_usd DOUBLE NOT NULL,
                    first_seen_ms BIGINT NOT NULL,
                    last_seen_ms BIGINT NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS signal_outcomes (
                    decision_id VARCHAR PRIMARY KEY,
                    symbol VARCHAR NOT NULL,
                    strategy_id VARCHAR NOT NULL,
                    side VARCHAR NOT NULL,
                    entry DOUBLE NOT NULL,
                    sl DOUBLE NOT NULL,
                    tp DOUBLE NOT NULL,
                    horizon_bars INTEGER NOT NULL,
                    result VARCHAR NOT NULL,
                    r_gross DOUBLE NOT NULL,
                    r_net DOUBLE NOT NULL,
                    bars_to_exit INTEGER NOT NULL,
                    mae_r DOUBLE NOT NULL,
                    mfe_r DOUBLE NOT NULL,
                    regime VARCHAR NOT NULL,
                    features_json VARCHAR,
                    resolved_at_ms BIGINT NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS trade_features (
                    position_id VARCHAR PRIMARY KEY,
                    strategy_id VARCHAR NOT NULL,
                    regime VARCHAR NOT NULL,
                    rsi DOUBLE NOT NULL,
                    prev_rsi DOUBLE NOT NULL,
                    adx DOUBLE NOT NULL,
                    atr_pct DOUBLE NOT NULL,
                    bb_dist_pct DOUBLE NOT NULL,
                    ema_dist_pct DOUBLE NOT NULL,
                    vol_ratio DOUBLE NOT NULL,
                    ob_imbalance DOUBLE NOT NULL,
                    spread_bps DOUBLE NOT NULL,
                    btc_trend_state VARCHAR NOT NULL,
                    hour_utc INTEGER NOT NULL,
                    side VARCHAR NOT NULL,
                    leverage INTEGER NOT NULL,
                    created_at_ms BIGINT NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS calibration_log (
                    id VARCHAR PRIMARY KEY,
                    old_threshold DOUBLE NOT NULL,
                    new_threshold DOUBLE NOT NULL,
                    reason VARCHAR NOT NULL,
                    n_samples INTEGER NOT NULL,
                    created_at_ms BIGINT NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS autonomy_log (
                    id VARCHAR PRIMARY KEY,
                    old_level INTEGER NOT NULL,
                    new_level INTEGER NOT NULL,
                    reason VARCHAR NOT NULL,
                    created_at_ms BIGINT NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS reconcile_events (
                    id VARCHAR PRIMARY KEY,
                    ts_ms BIGINT NOT NULL,
                    event_type VARCHAR NOT NULL,
                    symbol VARCHAR NOT NULL,
                    details_json VARCHAR,
                    resolved BOOLEAN NOT NULL
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
                "spot_free_usdt": "0.0",
                "futures_free_usdt": "0.0",
                "active_trading_mode": "SPOT",
                "peak_equity_usd": "0.0",
                "current_drawdown_pct": "0.0",
                "daily_realized_pnl_usd": "0.0",
                "auth_error_banner": "",
                "clock_drift_ms": "0",
                "paper_wallet_balance_usdt": str(getattr(settings, "PAPER_STARTING_USDT", 30.0)),
                "autonomy_level": str(getattr(settings, "AUTONOMY_START_LEVEL", 0)),
                "safe_mode": "false",
                "safe_mode_reason": "",
                "last_heartbeat_ms": str(int(time.time() * 1000)),
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

            # Idempotently add new columns to positions table if migrating existing DB
            new_pos_cols = [
                ("market_type", "VARCHAR DEFAULT 'spot'"),
                ("leverage", "INTEGER DEFAULT 1"),
                ("margin_usd", "DOUBLE DEFAULT 0.0"),
                ("notional_usd", "DOUBLE DEFAULT 0.0"),
                ("sl_slippage_pct", "DOUBLE DEFAULT 0.0"),
                ("is_paper", "BOOLEAN DEFAULT FALSE"),
                ("adopted", "BOOLEAN DEFAULT FALSE"),
                ("pnl_estimated", "BOOLEAN DEFAULT FALSE"),
            ]
            for col_name, col_def in new_pos_cols:
                try:
                    self._conn.execute(f"ALTER TABLE positions ADD COLUMN {col_name} {col_def}")
                except Exception:
                    pass  # Column already exists

            try:
                self._conn.execute("ALTER TABLE trade_lessons ADD COLUMN validated BOOLEAN DEFAULT TRUE")
            except Exception:
                pass

            try:
                self._conn.execute("ALTER TABLE reflex_decisions ADD COLUMN market_type VARCHAR")
            except Exception:
                pass
            try:
                self._conn.execute("ALTER TABLE reflex_decisions ADD COLUMN router_reason VARCHAR")
            except Exception:
                pass
            # Migrate legacy 0.80 threshold to new baseline if untouched by Brier calibration
            row_conf = self._conn.execute(
                "SELECT value FROM system_state WHERE key = 'min_confidence_threshold'"
            ).fetchone()
            if row_conf and row_conf[0] in ("0.8", "0.80") and settings.MIN_CONFIDENCE_THRESHOLD < 0.80:
                self._conn.execute(
                    "UPDATE system_state SET value = ?, updated_at_ms = ? WHERE key = 'min_confidence_threshold'",
                    [str(settings.MIN_CONFIDENCE_THRESHOLD), now_ms],
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
        self,
        decision: Dict[str, Any],
        executed: bool = False,
        outcome: Optional[int] = None,
        market_type: Optional[str] = None,
        router_reason: Optional[str] = None,
    ) -> str:
        now_ms = int(time.time() * 1000)
        dec_id = f"{decision['symbol']}_{decision['causal_timestamp_ms']}"
        m_type = market_type or decision.get("market_type")
        r_reason = router_reason or decision.get("router_reason")
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO reflex_decisions (
                    id, symbol, causal_timestamp_ms, regime, direction, toxic_flow,
                    setup_quality, direction_confidence, risk_state, entry_price,
                    invalidation_price, take_profit_price, expected_reward_risk_ratio,
                    rationale, executed, outcome, market_type, router_reason, created_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (id) DO UPDATE SET
                    executed = EXCLUDED.executed,
                    outcome = COALESCE(EXCLUDED.outcome, reflex_decisions.outcome),
                    market_type = COALESCE(EXCLUDED.market_type, reflex_decisions.market_type),
                    router_reason = COALESCE(EXCLUDED.router_reason, reflex_decisions.router_reason)
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
                    m_type,
                    r_reason,
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

    def get_open_orders(self) -> List[Dict[str, Any]]:
        with self._lock:
            cursor = self._conn.execute(
                "SELECT * FROM orders WHERE status = 'open' ORDER BY created_at_ms DESC"
            )
            cols = [d[0] for d in cursor.description]
            return [dict(zip(cols, r)) for r in cursor.fetchall()]

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
                    setup_quality, regime, opened_at_ms,
                    market_type, leverage, margin_usd, notional_usd, sl_slippage_pct, is_paper,
                    adopted, pnl_estimated
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0.0, 0.0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (position_id) DO UPDATE SET
                    status = EXCLUDED.status,
                    sl_client_order_id = COALESCE(EXCLUDED.sl_client_order_id, positions.sl_client_order_id),
                    sl_exchange_order_id = COALESCE(EXCLUDED.sl_exchange_order_id, positions.sl_exchange_order_id),
                    quantity = EXCLUDED.quantity,
                    cost_usd = EXCLUDED.cost_usd,
                    market_type = EXCLUDED.market_type,
                    leverage = EXCLUDED.leverage,
                    margin_usd = EXCLUDED.margin_usd,
                    notional_usd = EXCLUDED.notional_usd,
                    sl_slippage_pct = EXCLUDED.sl_slippage_pct,
                    is_paper = EXCLUDED.is_paper,
                    adopted = EXCLUDED.adopted,
                    pnl_estimated = EXCLUDED.pnl_estimated
                """,
                [
                    str(pos["position_id"]),
                    str(pos["symbol"]),
                    str(pos.get("status", "OPEN")),
                    str(pos.get("entry_client_order_id") or f"ENTRY_{pos['position_id']}"),
                    pos.get("entry_exchange_order_id"),
                    pos.get("sl_client_order_id"),
                    pos.get("sl_exchange_order_id"),
                    float(pos["entry_price"]),
                    float(pos.get("current_price", pos["entry_price"])),
                    float(pos.get("invalidation_price") or (float(pos["entry_price"]) * 0.98)),
                    float(pos.get("take_profit_price") or (float(pos["entry_price"]) * 1.03)),
                    float(pos["quantity"]),
                    float(pos["cost_usd"]),
                    float(pos.get("direction_confidence", 0.80)),
                    int(pos.get("setup_quality", 2)),
                    str(pos.get("regime", "trending")),
                    int(pos.get("opened_at_ms", now_ms)),
                    str(pos.get("market_type", "spot")),
                    int(pos.get("leverage", 1)),
                    float(pos.get("margin_usd", 0.0)),
                    float(pos.get("notional_usd", pos["cost_usd"])),
                    float(pos.get("sl_slippage_pct", 0.0)),
                    bool(pos.get("is_paper", False)),
                    bool(pos.get("adopted", False)),
                    bool(pos.get("pnl_estimated", False)),
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
        pnl_estimated: bool = False,
        realized_pnl: Optional[float] = None,
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

            if realized_pnl is not None:
                calc_pnl = float(realized_pnl)
            else:
                gross_pnl = (float(exit_price) - entry_price) * qty
                fees = (entry_price * qty + float(exit_price) * qty) * (fee_pct / 2.0)
                calc_pnl = gross_pnl - fees
            outcome = 1 if calc_pnl > 0.0 else 0

            self._conn.execute(
                """
                UPDATE positions
                SET status = 'CLOSED',
                    current_price = ?,
                    unrealized_pnl_usd = 0.0,
                    realized_pnl_usd = ?,
                    closed_at_ms = ?,
                    close_reason = ?,
                    pnl_estimated = ?
                WHERE position_id = ?
                """,
                [float(exit_price), float(calc_pnl), now_ms, close_reason, bool(pnl_estimated), position_id],
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

            if pos.get("is_paper"):
                m_type = str(pos.get("market_type", "spot")).lower()
                if m_type == "futures":
                    margin = float(pos.get("margin_usd", 0.0))
                    returned_cash = max(0.0, margin + realized_pnl)
                else:
                    exit_fee = float(exit_price) * qty * (fee_pct / 2.0)
                    returned_cash = max(0.0, (float(exit_price) * qty) - exit_fee)
                self.record_paper_ledger(
                    event=f"CLOSE_{m_type.upper()}",
                    amount=returned_cash,
                    symbol=pos.get("symbol"),
                    details={
                        "exit_price": float(exit_price),
                        "realized_pnl": realized_pnl,
                        "close_reason": close_reason,
                    },
                )

            pos["status"] = "CLOSED"
            pos["realized_pnl_usd"] = realized_pnl
            pos["closed_at_ms"] = now_ms
            pos["close_reason"] = close_reason
            return pos

    def record_paper_ledger(
        self,
        event: str,
        amount: float,
        symbol: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> float:
        with self._lock:
            current_wallet = float(self.get_state("paper_wallet_balance_usdt", "30.0") or 30.0)
            new_wallet = max(0.0, current_wallet + float(amount))
            self.set_state("paper_wallet_balance_usdt", f"{new_wallet:.4f}")
            now_ms = int(time.time() * 1000)
            entry_id = f"LEDGER_{now_ms}_{uuid.uuid4().hex[:6]}"
            self._conn.execute(
                """
                INSERT INTO paper_ledger (id, ts_ms, event, symbol, amount, balance_after, details_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [entry_id, now_ms, event, symbol, float(amount), new_wallet, json.dumps(details or {})],
            )
            return new_wallet

    def get_paper_ledger(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock:
            cursor = self._conn.execute(
                "SELECT * FROM paper_ledger ORDER BY ts_ms DESC LIMIT ?", [limit]
            )
            cols = [d[0] for d in cursor.description]
            return [dict(zip(cols, r)) for r in cursor.fetchall()]

    def add_reconcile_event(
        self,
        event_type: str,
        symbol: str,
        details: Optional[Dict[str, Any]] = None,
        resolved: bool = False,
    ) -> str:
        with self._lock:
            now_ms = int(time.time() * 1000)
            event_id = f"REC_{now_ms}_{uuid.uuid4().hex[:6]}"
            self._conn.execute(
                """
                INSERT INTO reconcile_events (id, ts_ms, event_type, symbol, details_json, resolved)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [event_id, now_ms, event_type, symbol, json.dumps(details or {}), bool(resolved)],
            )
            return event_id

    def get_reconcile_events(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock:
            cursor = self._conn.execute(
                "SELECT * FROM reconcile_events ORDER BY ts_ms DESC LIMIT ?", [limit]
            )
            cols = [d[0] for d in cursor.description]
            return [dict(zip(cols, r)) for r in cursor.fetchall()]

    def get_open_positions(self, include_unprotected: bool = True) -> List[Dict[str, Any]]:
        with self._lock:
            if include_unprotected:
                query = "SELECT * FROM positions WHERE status IN ('OPEN', 'OPEN_UNPROTECTED') ORDER BY opened_at_ms DESC"
            else:
                query = "SELECT * FROM positions WHERE status = 'OPEN' ORDER BY opened_at_ms DESC"
            cursor = self._conn.execute(query)
            cols = [d[0] for d in cursor.description]
            return [dict(zip(cols, r)) for r in cursor.fetchall()]

    def get_position(self, position_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            cursor = self._conn.execute(
                "SELECT * FROM positions WHERE position_id = ?", [str(position_id)]
            )
            row = cursor.fetchone()
            if not row:
                return None
            cols = [d[0] for d in cursor.description]
            return dict(zip(cols, row))

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
                WHERE status = 'CLOSED' AND closed_at_ms >= ? AND pnl_estimated IS NOT TRUE
                """,
                [start_of_day_ms],
            ).fetchone()
            return float(row[0]) if row else 0.0

    def get_daily_trades_count(self) -> int:
        """Return total number of positions opened during the current UTC day."""
        start_of_day_ms = int((int(time.time()) // 86400) * 86400 * 1000)
        with self._lock:
            row = self._conn.execute(
                """
                SELECT COUNT(*)
                FROM positions
                WHERE opened_at_ms >= ?
                """,
                [start_of_day_ms],
            ).fetchone()
            return int(row[0]) if row else 0

    def get_latest_state_snapshot(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Retrieve the most recent state snapshot for a symbol."""
        with self._lock:
            cursor = self._conn.execute(
                """
                SELECT * FROM state_snapshots
                WHERE symbol = ?
                ORDER BY causal_timestamp_ms DESC
                LIMIT 1
                """,
                [symbol],
            )
            row = cursor.fetchone()
            if not row:
                return None
            cols = [d[0] for d in cursor.description]
            return dict(zip(cols, row))

    def add_trade_lesson(self, lesson: Dict[str, Any]) -> None:
        """Persist a Stop-Loss failure lesson in the trade_lessons table."""
        now_ms = int(time.time() * 1000)
        lesson_id = str(lesson.get("id") or f"{lesson['symbol']}_{now_ms}")
        validated = bool(lesson.get("validated", True))
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO trade_lessons (
                    id, symbol, regime, entry_rsi, orderbook_imbalance,
                    pnl, failure_reason, confidence_penalty, created_at, validated
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (id) DO UPDATE SET
                    failure_reason = EXCLUDED.failure_reason,
                    confidence_penalty = EXCLUDED.confidence_penalty,
                    created_at = EXCLUDED.created_at,
                    validated = EXCLUDED.validated
                """,
                [
                    lesson_id,
                    str(lesson["symbol"]),
                    str(lesson.get("regime", "mean_reverting")),
                    float(lesson.get("entry_rsi", 50.0)),
                    float(lesson.get("orderbook_imbalance", 0.0)),
                    float(lesson.get("pnl", 0.0)),
                    str(lesson.get("failure_reason", "STOP_LOSS_HIT")),
                    float(lesson.get("confidence_penalty", 0.05)),
                    int(lesson.get("created_at", now_ms)),
                    validated,
                ],
            )

    def get_trade_lessons(
        self,
        symbol: Optional[str] = None,
        regime: Optional[str] = None,
        validated_only: bool = True,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """Fetch recent learned rules/mistakes from trade_lessons."""
        query = (
            "SELECT id, symbol, regime, entry_rsi, orderbook_imbalance, pnl, "
            "failure_reason, confidence_penalty, created_at, validated FROM trade_lessons"
        )
        params: List[Any] = []
        clauses: List[str] = []
        if validated_only:
            clauses.append("validated = TRUE")
        if symbol:
            clauses.append("symbol = ?")
            params.append(symbol)
        if regime:
            clauses.append("regime = ?")
            params.append(regime)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(int(limit))
        with self._lock:
            cursor = self._conn.execute(query, params)
            cols = [d[0] for d in cursor.description]
            return [dict(zip(cols, r)) for r in cursor.fetchall()]

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

    def save_signal_outcome(self, outcome_data: Dict[str, Any]) -> None:
        now_ms = int(time.time() * 1000)
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO signal_outcomes (
                    decision_id, symbol, strategy_id, side, entry, sl, tp,
                    horizon_bars, result, r_gross, r_net, bars_to_exit,
                    mae_r, mfe_r, regime, features_json, resolved_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (decision_id) DO UPDATE SET
                    result = EXCLUDED.result,
                    r_gross = EXCLUDED.r_gross,
                    r_net = EXCLUDED.r_net,
                    bars_to_exit = EXCLUDED.bars_to_exit,
                    mae_r = EXCLUDED.mae_r,
                    mfe_r = EXCLUDED.mfe_r,
                    features_json = EXCLUDED.features_json,
                    resolved_at_ms = EXCLUDED.resolved_at_ms
                """,
                [
                    str(outcome_data["decision_id"]),
                    str(outcome_data["symbol"]),
                    str(outcome_data.get("strategy_id", "default")),
                    str(outcome_data.get("side", "long")),
                    float(outcome_data["entry"]),
                    float(outcome_data["sl"]),
                    float(outcome_data["tp"]),
                    int(outcome_data.get("horizon_bars", 32)),
                    str(outcome_data["result"]),
                    float(outcome_data.get("r_gross", 0.0)),
                    float(outcome_data.get("r_net", 0.0)),
                    int(outcome_data.get("bars_to_exit", 0)),
                    float(outcome_data.get("mae_r", 0.0)),
                    float(outcome_data.get("mfe_r", 0.0)),
                    str(outcome_data.get("regime", "mean_reverting")),
                    json.dumps(outcome_data.get("features", {})),
                    int(outcome_data.get("resolved_at_ms", now_ms)),
                ],
            )

    def get_signal_outcomes(self, limit: int = 100) -> List[Dict[str, Any]]:
        with self._lock:
            cursor = self._conn.execute(
                "SELECT * FROM signal_outcomes ORDER BY resolved_at_ms DESC LIMIT ?",
                [int(limit)],
            )
            cols = [d[0] for d in cursor.description]
            return [dict(zip(cols, r)) for r in cursor.fetchall()]

    def save_trade_features(self, feat: Dict[str, Any]) -> None:
        now_ms = int(time.time() * 1000)
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO trade_features (
                    position_id, strategy_id, regime, rsi, prev_rsi, adx,
                    atr_pct, bb_dist_pct, ema_dist_pct, vol_ratio, ob_imbalance,
                    spread_bps, btc_trend_state, hour_utc, side, leverage, created_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (position_id) DO NOTHING
                """,
                [
                    str(feat["position_id"]),
                    str(feat.get("strategy_id", "default")),
                    str(feat.get("regime", "mean_reverting")),
                    float(feat.get("rsi", 50.0)),
                    float(feat.get("prev_rsi", 50.0)),
                    float(feat.get("adx", 20.0)),
                    float(feat.get("atr_pct", 1.0)),
                    float(feat.get("bb_dist_pct", 0.0)),
                    float(feat.get("ema_dist_pct", 0.0)),
                    float(feat.get("vol_ratio", 1.0)),
                    float(feat.get("ob_imbalance", 0.0)),
                    float(feat.get("spread_bps", 0.0)),
                    str(feat.get("btc_trend_state", "neutral")),
                    int(feat.get("hour_utc", 0)),
                    str(feat.get("side", "long")),
                    int(feat.get("leverage", 1)),
                    int(feat.get("created_at_ms", now_ms)),
                ],
            )

    def get_trade_features(self, position_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            cursor = self._conn.execute(
                "SELECT * FROM trade_features WHERE position_id = ?",
                [position_id],
            )
            row = cursor.fetchone()
            if not row:
                return None
            cols = [d[0] for d in cursor.description]
            return dict(zip(cols, row))

    def log_calibration_change(
        self, old_threshold: float, new_threshold: float, reason: str, n_samples: int
    ) -> None:
        now_ms = int(time.time() * 1000)
        cal_id = f"CAL_{now_ms}_{uuid.uuid4().hex[:6]}"
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO calibration_log (
                    id, old_threshold, new_threshold, reason, n_samples, created_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    cal_id,
                    float(old_threshold),
                    float(new_threshold),
                    str(reason),
                    int(n_samples),
                    now_ms,
                ],
            )
            self.set_state("min_confidence_threshold", str(round(new_threshold, 4)))

    def set_autonomy_level(self, new_level: int, reason: str) -> None:
        now_ms = int(time.time() * 1000)
        old_level = int(self.get_state("autonomy_level", "0") or 0)
        log_id = f"AUTO_{now_ms}_{uuid.uuid4().hex[:6]}"
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO autonomy_log (id, old_level, new_level, reason, created_at_ms)
                VALUES (?, ?, ?, ?, ?)
                """,
                [log_id, old_level, int(new_level), str(reason), now_ms],
            )
            self.set_state("autonomy_level", str(int(new_level)))

    def get_autonomy_level(self) -> int:
        return int(self.get_state("autonomy_level", "0") or 0)

    def set_safe_mode(self, enabled: bool, reason: str = "") -> None:
        with self._lock:
            self.set_state("safe_mode", "true" if enabled else "false")
            if reason:
                self.set_state("safe_mode_reason", str(reason))

    def is_safe_mode(self) -> bool:
        return self.get_state("safe_mode", "false").lower() in ("true", "1")

    def update_heartbeat(self) -> None:
        now_ms = int(time.time() * 1000)
        self.set_state("last_heartbeat_ms", str(now_ms))

    def upsert_dust_ignored(self, symbol: str, value_usd: float) -> bool:
        """Record ignored dust balance; return True only on first sighting or meaningful value increase."""
        now_ms = int(time.time() * 1000)
        with self._lock:
            row = self._conn.execute(
                "SELECT value_usd FROM dust_ignored WHERE symbol = ?",
                [symbol],
            ).fetchone()
            if row is None:
                self._conn.execute(
                    """
                    INSERT INTO dust_ignored (symbol, value_usd, first_seen_ms, last_seen_ms)
                    VALUES (?, ?, ?, ?)
                    """,
                    [symbol, float(value_usd), now_ms, now_ms],
                )
                return True
            prev_val = float(row[0] or 0.0)
            increased = float(value_usd) > max(prev_val * 1.15, prev_val + 0.05)
            self._conn.execute(
                """
                UPDATE dust_ignored SET value_usd = ?, last_seen_ms = ? WHERE symbol = ?
                """,
                [float(value_usd), now_ms, symbol],
            )
            return increased

    def close(self) -> None:
        with self._lock:
            self._conn.close()
