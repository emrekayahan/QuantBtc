"""Mistake Learning Loop: analyzes Stop-Loss exits, records failure patterns in DuckDB, and penalizes repeat mistakes."""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from database.db_manager import DatabaseManager
from engine.openrouter_pool import OpenRouterPool

logger = logging.getLogger(__name__)


class MistakeLearner:
    """Learns from Stop-Loss hits by recording structured failure lessons and applying confidence penalties."""

    def __init__(
        self,
        db: DatabaseManager,
        openrouter_pool: Optional[OpenRouterPool] = None,
        telegram: Optional[Any] = None,
    ) -> None:
        self.db = db
        self.openrouter_pool = openrouter_pool
        self.telegram = telegram

    def evaluate_setup_against_lessons(
        self,
        symbol: str,
        regime: str,
        entry_rsi: float,
        orderbook_imbalance: float,
    ) -> Dict[str, Any]:
        """
        Consult the `trade_lessons` table before executing a trade.
        Returns:
            {
                "allowed": bool,
                "confidence_penalty": float,
                "reason": str,
                "matched_lessons": int
            }
        """
        now_ms = int(time.time() * 1000)
        recent_symbol_lessons = self.db.get_trade_lessons(symbol=symbol, limit=10)

        penalty = 0.0
        matched = 0
        reasons: List[str] = []

        for lesson in recent_symbol_lessons:
            age_ms = now_ms - int(lesson.get("created_at", 0))
            lesson_regime = str(lesson.get("regime", ""))
            lesson_rsi = float(lesson.get("entry_rsi", 50.0))
            lesson_ob = float(lesson.get("orderbook_imbalance", 0.0))
            lesson_pen = float(lesson.get("confidence_penalty", 0.05))

            # If this exact symbol + regime failed on Stop-Loss in the last 2 hours, block immediate re-entry
            if age_ms <= 2 * 3600 * 1000 and lesson_regime == regime:
                return {
                    "allowed": False,
                    "confidence_penalty": lesson_pen,
                    "reason": (
                        f"Recent SL failure on {symbol} in {regime} regime "
                        f"({int(age_ms / 60000)}m ago: {lesson.get('failure_reason')})"
                    ),
                    "matched_lessons": 1,
                }

            # Apply decaying confidence penalty for similar setups within the last 48 hours
            if age_ms <= 48 * 3600 * 1000 and lesson_regime == regime:
                rsi_close = abs(entry_rsi - lesson_rsi) <= 6.0
                ob_similar = (orderbook_imbalance < 0 and lesson_ob < 0) or abs(orderbook_imbalance - lesson_ob) <= 0.15
                if rsi_close or ob_similar:
                    penalty += lesson_pen
                    matched += 1
                    reasons.append(str(lesson.get("failure_reason", "Similar past SL pattern")))

        penalty = min(0.15, round(penalty, 4))
        return {
            "allowed": True,
            "confidence_penalty": penalty,
            "reason": "; ".join(reasons[:2]) if reasons else "No conflicting failure lessons",
            "matched_lessons": matched,
        }

    async def analyze_and_record_stop_loss(
        self,
        closed_pos: Dict[str, Any],
        exit_reason: str = "SERVER_SIDE_STOP_LOSS_FILLED",
    ) -> Dict[str, Any]:
        """
        Analyze entry vs exit conditions when a position hits Stop-Loss,
        store the lesson in DuckDB `trade_lessons`, and send a Lesson Learned report to Telegram.
        """
        symbol = str(closed_pos.get("symbol", "UNKNOWN"))
        pos_id = str(closed_pos.get("position_id", f"{symbol}_{int(time.time()*1000)}"))
        regime = str(closed_pos.get("regime") or self.db.get_state("macro_regime", "mean_reverting") or "mean_reverting")
        pnl = float(closed_pos.get("realized_pnl_usd", 0.0))
        entry_price = float(closed_pos.get("entry_price", 0.0))
        exit_price = float(closed_pos.get("current_price") or closed_pos.get("invalidation_price") or 0.0)

        snap = self.db.get_latest_state_snapshot(symbol) or {}
        entry_rsi = float(snap.get("rsi_14", 50.0))
        orderbook_imbalance = float(snap.get("orderbook_imbalance", 0.0))

        # Deterministic root-cause diagnosis
        if orderbook_imbalance < -0.10:
            failure_reason = (
                f"Satis baskisina karsi giris (Orderbook Imbalance={orderbook_imbalance:+.2f}); "
                f"kademelerde satici agirlikliydi."
            )
            confidence_penalty = 0.08
        elif regime in ("mean_reverting", "high_vol") and entry_rsi > 31.0:
            failure_reason = (
                f"Mean-Reverting rejimde erken dip denemesi (RSI={entry_rsi:.1f} > 31); "
                f"satis dalgasi tam tukenmeden girildi."
            )
            confidence_penalty = 0.06
        elif regime == "trending" and entry_rsi > 62.0:
            failure_reason = (
                f"Trending rejimde yorgun momentum (RSI={entry_rsi:.1f}); "
                f"lokal direnc kirilimi sahte kaldi."
            )
            confidence_penalty = 0.07
        else:
            failure_reason = (
                f"{regime.upper()} rejiminde yuksek volatilite kaynakli SL temasi "
                f"(RSI={entry_rsi:.1f}, OB={orderbook_imbalance:+.2f})."
            )
            confidence_penalty = 0.05

        if self.openrouter_pool is not None:
            prompt = (
                f"A crypto trade hit Stop-Loss. Symbol={symbol}, Regime={regime}, "
                f"Entry={entry_price}, Exit={exit_price}, PnL=${pnl:.2f}, "
                f"RSI={entry_rsi:.1f}, OrderbookImbalance={orderbook_imbalance:+.2f}.\n"
                f"Write a 1-sentence Turkish lesson learned explaining why it failed and how to filter it next time."
            )
            try:
                res = await self.openrouter_pool.complete_with_fallback(
                    prompt=prompt,
                    deterministic_fallback=failure_reason,
                    max_tokens=140,
                )
                llm_text = str(res.get("content") or "").strip()
                if llm_text and not llm_text.lower().startswith(("the user", "we need", "trade details", "let's")):
                    failure_reason = llm_text[:280]
            except Exception as exc:
                logger.debug("MistakeLearner LLM fallback used: %s", exc)

        now_ms = int(time.time() * 1000)
        lesson = {
            "id": f"lesson_{pos_id}_{now_ms}",
            "symbol": symbol,
            "regime": regime,
            "entry_rsi": round(entry_rsi, 2),
            "orderbook_imbalance": round(orderbook_imbalance, 4),
            "pnl": round(pnl, 4),
            "failure_reason": failure_reason,
            "confidence_penalty": confidence_penalty,
            "created_at": now_ms,
        }
        self.db.add_trade_lesson(lesson)
        logger.info(
            "MistakeLearner recorded lesson for %s: penalty=-%.2f | %s",
            symbol,
            confidence_penalty,
            failure_reason,
        )

        if self.telegram is not None:
            msg = (
                f"📚 *DERS ÇIKARILDI (MISTAKE LEARNER)*\n\n"
                f"🪙 *Parite:* `{symbol}` (`{regime.upper()}`)\n"
                f"📉 *Sonuç:* `${pnl:+,.2f}` (`{exit_reason}`)\n"
                f"📊 *Giriş Metrikleri:* RSI `{entry_rsi:.1f}` | OB Imbalance `{orderbook_imbalance:+.2f}`\n"
                f"⚠️ *Hata Nedeni:* {failure_reason}\n"
                f"🛡️ *Yeni Kural:* Benzer `{symbol}` kurulumlarına `-%{confidence_penalty * 100:.0f}` güven cezası ve 2 saat soğuma uygulandı."
            )
            await self.telegram.send_message(msg)

        return lesson
