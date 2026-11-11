"""00:05 UTC Brier Score Calibration & Self-Tuning Loop."""

from __future__ import annotations

import datetime as dt
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from config.settings import Settings, settings
from database.db_manager import DatabaseManager
from engine.openrouter_pool import OpenRouterPool

logger = logging.getLogger(__name__)


class OvernightBrierReviewer:
    """Computes daily Brier Score at 00:05 UTC and self-tunes confidence threshold and position caps."""

    def __init__(
        self,
        db: DatabaseManager,
        openrouter_pool: Optional[OpenRouterPool] = None,
        custom_settings: Optional[Settings] = None,
    ) -> None:
        self.db = db
        self.openrouter_pool = openrouter_pool
        self.cfg = custom_settings or settings

    @staticmethod
    def compute_brier_score(samples: List[Tuple[float, int]]) -> float:
        """
        Compute Brier Score:
        BS = (1 / N) * sum((f_i - o_i)^2)
        where f_i is direction_confidence and o_i in {0, 1} is binary outcome.
        """
        if not samples:
            return 0.0
        total = sum((float(f) - int(o)) ** 2 for f, o in samples)
        return round(total / len(samples), 6)

    async def run_overnight_review(self) -> Dict[str, Any]:
        """
        1. Fetch resolved signals from signal_outcomes and closed positions.
        2. If samples < MIN_BRIER_SAMPLES (default 30):
           Mark status INSUFFICIENT_DATA, do NOT modify confidence threshold or position size,
           record brier_score as NULL in DB.
        3. If samples >= MIN_BRIER_SAMPLES:
           Compute Brier Score, reliability table buckets (0.6-0.7, 0.7-0.8, 0.8+),
           tune min_confidence_threshold and log in calibration_log.
        """
        min_samples = int(getattr(self.cfg, "MIN_BRIER_SAMPLES", 30))
        now_ms = int(time.time() * 1000)
        since_24h_ms = now_ms - 86_400_000

        # Query signal_outcomes resolved in last 24h
        outcomes_query = """
            SELECT s.*, r.direction_confidence
            FROM signal_outcomes s
            JOIN reflex_decisions r ON s.decision_id = r.id
            WHERE s.resolved_at_ms >= ?
        """
        samples: List[Tuple[float, int]] = []
        wins = 0
        total_pnl = 0.0

        with self.db._lock:
            cursor = self.db._conn.execute(outcomes_query, [since_24h_ms])
            cols = [d[0] for d in cursor.description]
            resolved_signals = [dict(zip(cols, r)) for r in cursor.fetchall()]

        for sig in resolved_signals:
            conf = float(sig.get("direction_confidence", 0.75))
            res = str(sig.get("result", "")).upper()
            is_win = 1 if res == "TP" or float(sig.get("r_net", 0.0)) > 0.0 else 0
            if is_win == 1:
                wins += 1
            samples.append((conf, is_win))

        # Also fallback to closed_positions if signal_outcomes is low
        if len(samples) < min_samples:
            closed_positions = self.db.get_closed_positions_since(since_24h_ms)
            for pos in closed_positions:
                conf = float(pos.get("direction_confidence", 0.80))
                pnl = float(pos.get("realized_pnl_usd", 0.0))
                is_win = 1 if pnl > 0.0 else 0
                total_pnl += pnl
                samples.append((conf, is_win))

        sample_count = len(samples)
        current_min_conf = float(
            self.db.get_state("min_confidence_threshold", str(self.cfg.MIN_CONFIDENCE_THRESHOLD))
            or self.cfg.MIN_CONFIDENCE_THRESHOLD
        )
        current_max_pos = float(
            self.db.get_state("max_position_usd", str(self.cfg.MAX_POSITION_USD))
            or self.cfg.MAX_POSITION_USD
        )

        today_str = dt.datetime.now(dt.timezone.utc).strftime("%Y_%m_%d")

        # R11 GUARD: If sample_count < MIN_BRIER_SAMPLES, status is INSUFFICIENT_DATA and NO changes made
        if sample_count < min_samples:
            calibration_action = (
                f"INSUFFICIENT_DATA: Sample size (N={sample_count} < {min_samples} required). "
                f"Retaining active parameters MIN_CONFIDENCE_THRESHOLD={current_min_conf:.2f}, "
                f"MAX_POSITION_USD=${current_max_pos:.2f}. Brier calibration skipped."
            )
            notes = f"# Overnight Quantitative Audit ({today_str})\n\n- **Status:** `INSUFFICIENT_DATA`\n- **Sample Count:** {sample_count}/{min_samples}\n- {calibration_action}"
            log_dir = Path(self.cfg.LOG_DIR)
            log_dir.mkdir(parents=True, exist_ok=True)
            audit_file = log_dir / f"overnight_audit_{today_str}.md"
            audit_file.write_text(notes, encoding="utf-8")

            return {
                "status": "INSUFFICIENT_DATA",
                "review_date": today_str,
                "sample_count": sample_count,
                "brier_score": None,
                "min_confidence_threshold": current_min_conf,
                "max_position_usd": current_max_pos,
                "notes": notes,
                "audit_file": str(audit_file),
                "autonomy_promoted": False,
            }

        # Calculate Brier score and reliability table
        brier_score = self.compute_brier_score(samples)

        # Reliability table
        bucket_60_70 = [o for f, o in samples if 0.60 <= f < 0.70]
        bucket_70_80 = [o for f, o in samples if 0.70 <= f < 0.80]
        bucket_80_plus = [o for f, o in samples if f >= 0.80]

        acc_60_70 = (sum(bucket_60_70) / len(bucket_60_70)) if bucket_60_70 else 0.0
        acc_70_80 = (sum(bucket_70_80) / len(bucket_70_80)) if bucket_70_80 else 0.0
        acc_80_plus = (sum(bucket_80_plus) / len(bucket_80_plus)) if bucket_80_plus else 0.0

        if brier_score > 0.25:
            # Overconfident: raise threshold, do not exceed 0.85
            new_min_conf = min(0.85, round(current_min_conf + 0.02, 4))
            new_max_pos = current_max_pos  # MAX_POSITION_USD cannot be increased automatically
            calibration_action = (
                f"Overconfident Brier Score ({brier_score:.4f} > 0.25): "
                f"Raised MIN_CONFIDENCE_THRESHOLD {current_min_conf:.2f} -> {new_min_conf:.2f}."
            )
            self.db.log_calibration_change(
                old_threshold=current_min_conf,
                new_threshold=new_min_conf,
                reason=f"Overconfident Brier ({brier_score:.4f})",
                n_samples=sample_count,
            )
        elif brier_score < 0.18 and current_min_conf > 0.60:
            # Well-calibrated with hysteresis: reduce cautiously
            new_min_conf = max(0.60, round(current_min_conf - 0.02, 4))
            new_max_pos = current_max_pos
            calibration_action = (
                f"Well-calibrated Brier Score ({brier_score:.4f} < 0.18): "
                f"Lowered MIN_CONFIDENCE_THRESHOLD {current_min_conf:.2f} -> {new_min_conf:.2f}."
            )
            self.db.log_calibration_change(
                old_threshold=current_min_conf,
                new_threshold=new_min_conf,
                reason=f"Well-calibrated Brier ({brier_score:.4f})",
                n_samples=sample_count,
            )
        else:
            new_min_conf = current_min_conf
            new_max_pos = current_max_pos
            calibration_action = (
                f"Nominal Brier Score ({brier_score:.4f}): "
                f"Maintained MIN_CONFIDENCE_THRESHOLD={new_min_conf:.2f}."
            )

        reliability_str = (
            f"| Confidence Bucket | Count | Realized Accuracy |\n"
            f"|---|---|---|\n"
            f"| 0.60 - 0.70 | {len(bucket_60_70)} | {acc_60_70 * 100:.1f}% |\n"
            f"| 0.70 - 0.80 | {len(bucket_70_80)} | {acc_70_80 * 100:.1f}% |\n"
            f"| 0.80+ | {len(bucket_80_plus)} | {acc_80_plus * 100:.1f}% |\n"
        )

        deterministic_md = (
            f"# Overnight Quantitative Audit ({today_str})\n\n"
            f"- **Sample Count (24h):** {sample_count}\n"
            f"- **Win Rate:** {(wins / sample_count * 100.0) if sample_count else 0.0:.1f}%\n"
            f"- **Brier Score:** `{brier_score:.4f}`\n"
            f"- **Self-Tuning Action:** {calibration_action}\n\n"
            f"### Reliability Table\n{reliability_str}\n"
        )

        notes = deterministic_md
        if self.openrouter_pool is not None:
            res = await self.openrouter_pool.complete_with_fallback(
                prompt=f"Summarize this 24h quantitative trading audit in concise Markdown:\n{deterministic_md}",
                deterministic_fallback=deterministic_md,
                max_tokens=280,
            )
            notes = res["content"]

        self.db.save_brier_calibration(
            review_date=today_str,
            sample_count=sample_count,
            brier_score=float(brier_score),
            min_confidence_threshold=new_min_conf,
            max_position_usd=new_max_pos,
            notes=notes,
        )

        log_dir = Path(self.cfg.LOG_DIR)
        log_dir.mkdir(parents=True, exist_ok=True)
        audit_file = log_dir / f"overnight_audit_{today_str}.md"
        audit_file.write_text(notes, encoding="utf-8")

        # S8: Autonomy Level Auto-Promotion check
        # Only promote from Level 1 to Level 2 if signal_outcomes has n >= 100 and lower-bound 95% CI of net R >= 0
        autonomy_promoted = False
        current_level = self.db.get_autonomy_level()
        if current_level == 1:
            all_outcomes = self.db.get_signal_outcomes(limit=1000)
            if len(all_outcomes) >= 100:
                r_nets = [float(o.get("r_net", 0.0)) for o in all_outcomes if o.get("r_net") is not None]
                if len(r_nets) >= 100:
                    n_r = len(r_nets)
                    mean_r = sum(r_nets) / n_r
                    variance_r = sum((x - mean_r) ** 2 for x in r_nets) / max(n_r - 1, 1)
                    std_err = (variance_r / n_r) ** 0.5
                    lower_bound_ci = mean_r - (1.96 * std_err)
                    if lower_bound_ci >= 0.0:
                        self.db.set_autonomy_level(
                            2,
                            f"Auto-promoted from L1 to L2: n={n_r}, mean_R={mean_r:.3f}, 95% CI lower-bound={lower_bound_ci:.3f} >= 0",
                        )
                        autonomy_promoted = True
                        logger.info(
                            "Autonomy promoted to Level 2! (n=%d, mean_R=%.3f, CI_lower=%.3f)",
                            n_r, mean_r, lower_bound_ci
                        )
                    else:
                        logger.info(
                            "Autonomy Level 1 maintained: lower-bound 95% CI (%.3f) < 0 (n=%d, mean_R=%.3f)",
                            lower_bound_ci, n_r, mean_r
                        )
            else:
                logger.info(
                    "Autonomy Level 1 maintained: signal outcomes sample count (%d) < 100",
                    len(all_outcomes),
                )

        return {
            "status": "CALIBRATED",
            "review_date": today_str,
            "sample_count": sample_count,
            "brier_score": brier_score,
            "min_confidence_threshold": new_min_conf,
            "max_position_usd": new_max_pos,
            "notes": notes,
            "audit_file": str(audit_file),
            "autonomy_promoted": autonomy_promoted,
        }
