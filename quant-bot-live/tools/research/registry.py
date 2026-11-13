"""K1 ExperimentRegistry: tracks every backtest trial and enforces max configs per strategy."""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import duckdb

from config.settings import Settings, settings


class ExperimentRegistry:
    """DuckDB-backed registry preventing unlogged trials and p-hacking (>12 configs/strategy)."""

    def __init__(
        self,
        db_path: Optional[str] = None,
        max_configs_per_strategy: Optional[int] = None,
        custom_settings: Optional[Settings] = None,
    ) -> None:
        cfg = custom_settings or settings
        self.max_configs = int(
            max_configs_per_strategy
            if max_configs_per_strategy is not None
            else cfg.RESEARCH_MAX_CONFIGS_PER_STRATEGY
        )
        default_path = os.path.join(cfg.RESEARCH_DATA_DIR, "experiments.db")
        self.db_path = db_path or default_path
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = duckdb.connect(self.db_path)
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS experiment_registry (
                trial_id VARCHAR PRIMARY KEY,
                strategy_id VARCHAR NOT NULL,
                config_hash VARCHAR NOT NULL,
                config_json VARCHAR NOT NULL,
                seed INTEGER NOT NULL,
                n_trades INTEGER NOT NULL,
                mean_net_r DOUBLE NOT NULL,
                lcb_80 DOUBLE NOT NULL,
                g1_passed BOOLEAN NOT NULL,
                cost_breakdown_json VARCHAR NOT NULL,
                created_at_ms BIGINT NOT NULL
            )
            """
        )

    def get_trial_count(self, strategy_id: str) -> int:
        row = self._conn.execute(
            "SELECT count(*) FROM experiment_registry WHERE strategy_id = ?",
            [strategy_id],
        ).fetchone()
        return int(row[0] or 0) if row else 0

    def log_trial(
        self,
        strategy_id: str,
        config: Dict[str, Any],
        seed: int,
        n_trades: int,
        mean_net_r: float,
        lcb_80: float,
        g1_passed: bool,
        cost_breakdown: Dict[str, Any],
    ) -> str:
        current_count = self.get_trial_count(strategy_id)
        if current_count >= self.max_configs:
            raise ValueError(
                f"Strategy '{strategy_id}' exceeded RESEARCH_MAX_CONFIGS_PER_STRATEGY={self.max_configs}"
            )
        cfg_json = json.dumps(config, sort_keys=True)
        cfg_hash = hashlib.sha256(cfg_json.encode("utf-8")).hexdigest()[:16]
        now_ms = int(time.time() * 1000)
        trial_id = f"{strategy_id}_{cfg_hash}_{seed}_{current_count + 1}"
        self._conn.execute(
            """
            INSERT INTO experiment_registry (
                trial_id, strategy_id, config_hash, config_json, seed,
                n_trades, mean_net_r, lcb_80, g1_passed, cost_breakdown_json, created_at_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                trial_id,
                strategy_id,
                cfg_hash,
                cfg_json,
                int(seed),
                int(n_trades),
                float(mean_net_r),
                float(lcb_80),
                bool(g1_passed),
                json.dumps(cost_breakdown, sort_keys=True),
                now_ms,
            ],
        )
        return trial_id

    def list_trials(self, strategy_id: Optional[str] = None) -> List[Dict[str, Any]]:
        if strategy_id:
            rows = self._conn.execute(
                "SELECT trial_id, strategy_id, config_hash, n_trades, mean_net_r, lcb_80, g1_passed FROM experiment_registry WHERE strategy_id = ? ORDER BY created_at_ms DESC",
                [strategy_id],
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT trial_id, strategy_id, config_hash, n_trades, mean_net_r, lcb_80, g1_passed FROM experiment_registry ORDER BY created_at_ms DESC"
            ).fetchall()
        return [
            {
                "trial_id": r[0],
                "strategy_id": r[1],
                "config_hash": r[2],
                "n_trades": r[3],
                "mean_net_r": r[4],
                "lcb_80": r[5],
                "g1_passed": r[6],
            }
            for r in rows
        ]

    def close(self) -> None:
        self._conn.close()
