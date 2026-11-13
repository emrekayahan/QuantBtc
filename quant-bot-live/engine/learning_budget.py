"""K8: LearningBudgetManager — G1/G2/G3 lifecycle, 15% rolling 14d budget, Thompson bandit."""
from __future__ import annotations

import time
import uuid
from typing import Any, Dict, Optional

import numpy as np

from config.settings import Settings, settings
from database.db_manager import DatabaseManager
from tools.research.gates import evaluate_g2_gate, evaluate_g3_gate, should_demote_to_suspended


class LearningBudgetManager:
    """Enforces G1/G2/G3 proof gates, 14-day rolling learning budget, and Thompson sampling."""

    def __init__(
        self,
        db: Optional[DatabaseManager] = None,
        custom_settings: Optional[Settings] = None,
    ) -> None:
        self.db = db
        self.cfg = custom_settings or settings

    def set_starting_equity(self, equity_usd: float) -> None:
        if self.db is not None:
            self.db.set_state("learning_budget_anchor_equity", str(float(equity_usd)))
            if not self.db.get_state("learning_budget_window_start_ms"):
                self.db.set_state("learning_budget_window_start_ms", str(int(time.time() * 1000)))

    def record_live_learn_pnl(self, pnl_usd: float, source: str = "live") -> None:
        """Record realized PnL separately by source ('live'|'paper'|'shadow')."""
        if self.db is None:
            return
        now_ms = int(time.time() * 1000)
        with self.db._lock:
            self.db._conn.execute(
                "INSERT INTO learning_budget_events (event_id, pnl_usd, source, ts_ms) VALUES (?, ?, ?, ?)",
                [f"LB_{uuid.uuid4().hex[:12]}", float(pnl_usd), str(source), now_ms],
            )

    def get_rolling_14d_live_loss_usd(self) -> float:
        if self.db is None:
            return 0.0
        now_ms = int(time.time() * 1000)
        window_start = int(self.db.get_state("learning_budget_window_start_ms") or 0)
        cutoff = max(window_start, now_ms - 14 * 86_400_000)
        row = self.db._conn.execute(
            "SELECT COALESCE(SUM(pnl_usd), 0.0) FROM learning_budget_events WHERE source = 'live' AND ts_ms >= ?",
            [cutoff],
        ).fetchone()
        net_pnl = float(row[0] or 0.0) if row else 0.0
        return max(0.0, -net_pnl)

    def is_budget_exhausted(self) -> bool:
        if self.db is None:
            return False
        anchor = float(self.db.get_state("learning_budget_anchor_equity") or 0.0)
        if anchor <= 0.0:
            return False
        max_loss = anchor * (float(self.cfg.LEARNING_BUDGET_PCT) / 100.0)
        return self.get_rolling_14d_live_loss_usd() >= max_loss

    def approve_budget_renewal(self, current_equity: float) -> None:
        """Operator approval (/onayla_ogrenme) resets the 14-day learning budget window."""
        if self.db is not None:
            self.db.set_state("learning_budget_anchor_equity", str(float(current_equity)))
            self.db.set_state("learning_budget_window_start_ms", str(int(time.time() * 1000)))

    def get_strategy_state(self, strategy_id: str) -> str:
        if self.db is None:
            return "shadow"
        row = self.db._conn.execute(
            "SELECT state FROM strategy_lifecycle WHERE strategy_id = ?",
            [strategy_id],
        ).fetchone()
        return str(row[0]) if row else "shadow"

    def can_enter_live_learn(self, strategy_id: str, g1_passed: bool) -> bool:
        if not g1_passed:
            return False
        if self.is_budget_exhausted():
            return False
        return True

    def transition_strategy(
        self,
        strategy_id: str,
        target_state: str,
        g1_passed: bool = False,
        live_stats: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Enforce Section 6 proof gates for every lifecycle state transition."""
        target = str(target_state).lower()
        allowed = False
        reason = f"G1={g1_passed}"

        if target == "shadow" or target == "suspended":
            allowed = True
        elif target == "learn":
            allowed = self.can_enter_live_learn(strategy_id, g1_passed=g1_passed)
        elif target == "exploit_low" and live_stats is not None:
            g2 = evaluate_g2_gate(
                n_live_trades=max(int(live_stats.get("n", 0)), int(self.cfg.G2_MIN_LIVE_TRADES))
                if int(live_stats.get("n", 0)) >= int(self.cfg.G2_MIN_LIVE_TRADES)
                else int(live_stats.get("n", 0)),
                p_mu_pos=float(live_stats.get("p_mu_pos", 0.0)),
                mu_mean=float(live_stats.get("mu_mean", 0.0)),
                max_drawdown_pct=float(live_stats.get("max_drawdown_pct", 0.0)),
                safety_violations=int(live_stats.get("safety_violations", 0)),
            )
            allowed = g2.passed and float(live_stats.get("p_mu_pos", 0.0)) >= float(self.cfg.G2_P_MU_POS)
            reason = "; ".join(g2.reasons) or "G2_PASS"
        elif target == "exploit_high" and live_stats is not None:
            g3 = evaluate_g3_gate(
                n_total_trades=int(live_stats.get("n", 0)),
                p_mu_pos=float(live_stats.get("p_mu_pos", 0.0)),
                mu_lcb=float(live_stats.get("mu_lcb", 0.0)),
                reconcile_violations=int(live_stats.get("reconcile_violations", 0)),
            )
            allowed = (
                g3.passed
                and int(live_stats.get("n", 0)) >= int(self.cfg.G3_MIN_LIVE_TRADES)
                and float(live_stats.get("p_mu_pos", 0.0)) >= float(self.cfg.G3_P_MU_POS)
            )
            reason = "; ".join(g3.reasons) or "G3_PASS"

        if not allowed:
            return False

        if self.db is not None:
            now_ms = int(time.time() * 1000)
            with self.db._lock:
                self.db._conn.execute(
                    """
                    INSERT INTO strategy_lifecycle (strategy_id, state, g1_passed, updated_at_ms, reason)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT (strategy_id) DO UPDATE SET
                        state = EXCLUDED.state,
                        g1_passed = EXCLUDED.g1_passed,
                        updated_at_ms = EXCLUDED.updated_at_ms,
                        reason = EXCLUDED.reason
                    """,
                    [strategy_id, target, bool(g1_passed), now_ms, reason],
                )
        return True

    def check_and_demote(self, strategy_id: str, rolling_30_net_r: float, p_mu_pos: float, n_trades: int) -> bool:
        if should_demote_to_suspended(rolling_30_net_r, p_mu_pos, n_trades):
            self.transition_strategy(strategy_id, "suspended")
            return True
        return False

    def remaining_learn_trades_today(self, used_today: int) -> int:
        return max(0, int(self.cfg.MAX_LEARN_TRADES_PER_DAY) - int(used_today))

    @staticmethod
    def select_thompson_strategy(
        posteriors: Dict[str, Dict[str, float]],
        seed: Optional[int] = None,
    ) -> str:
        """Pick strategy via Thompson sampling over posterior Normal(mu_mean, se)."""
        rng = np.random.default_rng(seed)
        best_id = ""
        best_draw = -1e9
        for sid, st in posteriors.items():
            draw = float(rng.normal(float(st.get("mu_mean", -0.05)), max(0.01, float(st.get("se", 0.3)))))
            if draw > best_draw:
                best_draw = draw
                best_id = sid
        return best_id
