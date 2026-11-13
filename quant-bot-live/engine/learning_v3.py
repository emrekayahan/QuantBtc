"""K7: LearningEngineV3 — Bayesian shrinkage posterior, deterministic pattern suspension, calibration."""
from __future__ import annotations

import math
import time
from statistics import NormalDist
from typing import Any, Dict, Optional, Sequence, Tuple

from database.db_manager import DatabaseManager


class LearningEngineV3:
    """Statistically grounded learning engine; LLM only summarizes and never decides."""

    PRIOR_MU = -0.05
    PRIOR_N = 10
    DEFAULT_SIGMA = 1.3
    Z_80 = 0.84162123

    def __init__(self, db: Optional[DatabaseManager] = None) -> None:
        self.db = db

    def compute_posterior(self, r_values: Sequence[float]) -> Dict[str, float]:
        """Compute shrinkage posterior (mu_mean, mu_lcb 80%, p_mu_pos, sigma_r, n)."""
        n = len(r_values)
        total_r = sum(float(x) for x in r_values)
        eff_n = self.PRIOR_N + n
        mu_mean = (self.PRIOR_N * self.PRIOR_MU + total_r) / eff_n

        if n >= 5:
            mean_s = total_r / n
            var = sum((float(x) - mean_s) ** 2 for x in r_values) / max(1, n - 1)
            sigma = max(0.25, math.sqrt(var))
        else:
            sigma = self.DEFAULT_SIGMA

        se = sigma / math.sqrt(eff_n)
        mu_lcb = mu_mean - self.Z_80 * se
        p_mu_pos = NormalDist().cdf(mu_mean / max(1e-9, se))
        return {
            "n": n,
            "mu_mean": round(mu_mean, 6),
            "mu_lcb": round(mu_lcb, 6),
            "sigma_r": round(sigma, 6),
            "se": round(se, 6),
            "p_mu_pos": round(p_mu_pos, 6),
        }

    @staticmethod
    def _format_pattern_key(key: Tuple[str, ...]) -> str:
        return "|".join(str(k) for k in key)

    def evaluate_pattern_suspension(
        self,
        pattern_key: Tuple[str, ...],
        r_values: Sequence[float],
        ttl_days: int = 7,
    ) -> bool:
        """Suspend pattern for 7 days if n >= 12 and 80% LCB < 0."""
        if len(r_values) < 12:
            return False
        post = self.compute_posterior(r_values)
        if post["mu_lcb"] < 0.0:
            if self.db is not None:
                now_ms = int(time.time() * 1000)
                until_ms = now_ms + int(ttl_days * 86_400_000)
                k_str = self._format_pattern_key(pattern_key)
                with self.db._lock:
                    self.db._conn.execute(
                        """
                        INSERT INTO suspended_patterns (pattern_key, n_trades, lcb_80, suspended_until_ms, updated_at_ms)
                        VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT (pattern_key) DO UPDATE SET
                            n_trades = EXCLUDED.n_trades,
                            lcb_80 = EXCLUDED.lcb_80,
                            suspended_until_ms = EXCLUDED.suspended_until_ms,
                            updated_at_ms = EXCLUDED.updated_at_ms
                        """,
                        [k_str, len(r_values), post["mu_lcb"], until_ms, now_ms],
                    )
            return True
        return False

    def is_pattern_suspended(self, pattern_key: Tuple[str, ...]) -> bool:
        if self.db is None:
            return False
        k_str = self._format_pattern_key(pattern_key)
        now_ms = int(time.time() * 1000)
        row = self.db._conn.execute(
            "SELECT suspended_until_ms FROM suspended_patterns WHERE pattern_key = ?",
            [k_str],
        ).fetchone()
        return bool(row and int(row[0]) > now_ms)

    @staticmethod
    def is_calibration_ready(score_outcome_pairs: Sequence[Tuple[float, float]]) -> bool:
        """Calibration is never used for sizing until n >= 100."""
        return len(score_outcome_pairs) >= 100

    def should_promote_challenger(
        self,
        challenger_r: Sequence[float],
        champion_r: Sequence[float],
    ) -> bool:
        """Promote challenger only when both have n >= 30 and challenger LCB exceeds champion mean."""
        if len(challenger_r) < 30 or len(champion_r) < 30:
            return False
        c_post = self.compute_posterior(challenger_r)
        k_post = self.compute_posterior(champion_r)
        return c_post["mu_lcb"] > k_post["mu_mean"]

    def record_signal_features(
        self,
        trade_id: str,
        symbol: str,
        side: str,
        regime: str,
        features: Dict[str, Any],
    ) -> None:
        if self.db is None:
            return
        self.db.save_trade_features(
            {
                "position_id": trade_id,
                "strategy_id": str(features.get("strategy_id", "default")),
                "side": side,
                "regime": regime,
                "rsi": float(features.get("rsi", 50.0)),
                "adx": float(features.get("adx", 20.0)),
                "atr_pct": float(features.get("atr_pct", 0.01)),
            }
        )

    @staticmethod
    def validate_human_summary(text: str, fallback: str = "Bugün yeni istatistiksel desen askıya alınmadı.") -> str:
        """Validate LLM-generated summary: <=200 chars, single sentence, no prompt echo."""
        clean = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
        lowered = clean.lower()
        if (
            not clean
            or len(clean) > 200
            or "system:" in lowered
            or "prompt" in lowered
            or "json" in lowered
        ):
            return fallback
        return clean
