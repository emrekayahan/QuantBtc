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
        1. Fetch resolved trades from the past 24 hours.
        2. Compute Brier Score.
        3. If BS > 0.25 (poorly calibrated), raise MIN_CONFIDENCE_THRESHOLD by +0.03 (up to 0.90)
           and reduce MAX_POSITION_USD by 20% for the next 24 hours.
        4. Write Markdown audit report to logs/overnight_audit_YYYY_MM_DD.md and persist in DuckDB.
        """
        now_ms = int(time.time() * 1000)
        since_24h_ms = now_ms - 86_400_000
        closed_positions = self.db.get_closed_positions_since(since_24h_ms)

        samples: List[Tuple[float, int]] = []
        wins = 0
        total_pnl = 0.0

        for pos in closed_positions:
            conf = float(pos.get("direction_confidence", 0.80))
            realized_pnl = float(pos.get("realized_pnl_usd", 0.0))
            outcome = 1 if realized_pnl > 0.0 else 0
            if outcome == 1:
                wins += 1
            total_pnl += realized_pnl
            samples.append((conf, outcome))

        sample_count = len(samples)
        brier_score = self.compute_brier_score(samples) if sample_count > 0 else None

        current_min_conf = float(
            self.db.get_state("min_confidence_threshold", str(self.cfg.MIN_CONFIDENCE_THRESHOLD))
            or self.cfg.MIN_CONFIDENCE_THRESHOLD
        )
        current_max_pos = float(
            self.db.get_state("max_position_usd", str(self.cfg.MAX_POSITION_USD))
            or self.cfg.MAX_POSITION_USD
        )

        if sample_count == 0:
            new_min_conf = current_min_conf
            new_max_pos = current_max_pos
            calibration_action = (
                "Insufficient sample size (N=0 resolved trades in 24h): "
                f"Retaining active parameters MIN_CONFIDENCE_THRESHOLD={current_min_conf:.2f}, "
                f"MAX_POSITION_USD=${current_max_pos:.2f}. Brier calibration skipped."
            )
        elif brier_score is not None and brier_score > 0.25:
            new_min_conf = min(0.90, round(current_min_conf + 0.03, 4))
            new_max_pos = round(max(11.5, current_max_pos * 0.80), 2)
            calibration_action = (
                f"Overconfident Brier Score ({brier_score:.4f} > 0.25): "
                f"Raised MIN_CONFIDENCE_THRESHOLD {current_min_conf:.2f} -> {new_min_conf:.2f}, "
                f"Reduced MAX_POSITION_USD ${current_max_pos:.2f} -> ${new_max_pos:.2f}."
            )
        else:
            # Gradually restore toward baseline defaults when well-calibrated
            new_min_conf = float(self.cfg.MIN_CONFIDENCE_THRESHOLD)
            new_max_pos = float(self.cfg.MAX_POSITION_USD)
            bs_str = f"{brier_score:.4f}" if brier_score is not None else "N/A"
            calibration_action = (
                f"Well-calibrated Brier Score ({bs_str} <= 0.25): "
                f"Maintained baseline MIN_CONFIDENCE_THRESHOLD={new_min_conf:.2f}, "
                f"MAX_POSITION_USD=${new_max_pos:.2f}."
            )

        today_str = dt.datetime.now(dt.timezone.utc).strftime("%Y_%m_%d")
        bs_display = f"`{brier_score:.4f}`" if brier_score is not None else "`N/A (insufficient samples)`"
        deterministic_md = (
            f"# Overnight Quantitative Audit ({today_str})\n\n"
            f"- **Sample Count (24h Resolved Trades):** {sample_count}\n"
            f"- **Win Rate:** {(wins / sample_count * 100.0) if sample_count else 0.0:.1f}%\n"
            f"- **24h Realized PnL:** ${total_pnl:.2f}\n"
            f"- **Brier Score:** {bs_display}\n"
            f"- **Self-Tuning Action:** {calibration_action}\n"
        )

        notes = deterministic_md
        if self.openrouter_pool is not None:
            res = await self.openrouter_pool.complete_with_fallback(
                prompt=(
                    f"Summarize this 24-hour quantitative trading audit in concise Markdown:\n"
                    f"{deterministic_md}"
                ),
                deterministic_fallback=deterministic_md,
                max_tokens=280,
            )
            notes = res["content"]

        self.db.save_brier_calibration(
            review_date=today_str,
            sample_count=sample_count,
            brier_score=float(brier_score) if brier_score is not None else 0.0,
            min_confidence_threshold=new_min_conf,
            max_position_usd=new_max_pos,
            notes=notes,
        )

        log_dir = Path(self.cfg.LOG_DIR)
        log_dir.mkdir(parents=True, exist_ok=True)
        audit_file = log_dir / f"overnight_audit_{today_str}.md"
        audit_file.write_text(notes, encoding="utf-8")

        return {
            "review_date": today_str,
            "sample_count": sample_count,
            "brier_score": brier_score,
            "min_confidence_threshold": new_min_conf,
            "max_position_usd": new_max_pos,
            "notes": notes,
            "audit_file": str(audit_file),
        }
