"""K9: Autonomy v2 — evidence & account-health state machine (NORMAL/CAUTION/HALT_DAY/COOLDOWN/SAFE_MODE)."""
from __future__ import annotations

import time
import uuid
from typing import Optional

from config.settings import Settings, settings
from database.db_manager import DatabaseManager


class AutonomyManagerV2:
    """Manages account health state transitions and logs every transition to state_log."""

    def __init__(
        self,
        db: DatabaseManager,
        custom_settings: Optional[Settings] = None,
    ) -> None:
        self.db = db
        self.cfg = custom_settings or settings

    def get_account_state(self) -> str:
        return self.db.get_state("account_state", "NORMAL")

    def get_risk_multiplier(self) -> float:
        state = self.get_account_state()
        if state == "NORMAL":
            return 1.0
        if state == "CAUTION":
            return 0.5
        return 0.0  # HALT_DAY, COOLDOWN, SAFE_MODE

    def _set_account_state(self, new_state: str, reason: str) -> None:
        old_state = self.get_account_state()
        if old_state == new_state:
            return
        now_ms = int(time.time() * 1000)
        self.db.set_state("account_state", new_state)
        with self.db._lock:
            self.db._conn.execute(
                "INSERT INTO state_log (id, old_state, new_state, reason, ts_ms) VALUES (?, ?, ?, ?, ?)",
                [f"ST_{uuid.uuid4().hex[:12]}", old_state, new_state, reason, now_ms],
            )

    def evaluate_account_state(self, drawdown_pct: float, daily_loss_pct: float) -> str:
        """Evaluate account state from drawdown, daily loss, and safe_mode."""
        if self.db.is_safe_mode():
            self._set_account_state("SAFE_MODE", "DB safe_mode active")
            return "SAFE_MODE"
        if float(drawdown_pct) >= 12.0:
            self.db.set_state("cooldown_until_ms", str(int(time.time() * 1000) + 86_400_000))
            self._set_account_state("COOLDOWN", f"Drawdown {drawdown_pct:.2f}% >= 12.0% (24h cooldown)")
            return "COOLDOWN"
        if float(daily_loss_pct) >= float(self.cfg.DAILY_MAX_LOSS_PCT):
            self._set_account_state("HALT_DAY", f"Daily loss {daily_loss_pct:.2f}% >= {self.cfg.DAILY_MAX_LOSS_PCT}%")
            return "HALT_DAY"
        if float(drawdown_pct) >= 6.0:
            self._set_account_state("CAUTION", f"Drawdown {drawdown_pct:.2f}% >= 6.0% (risk x0.5)")
            return "CAUTION"
        self._set_account_state("NORMAL", "Account metrics within healthy thresholds")
        return "NORMAL"
