"""Pre-Trade Deep Analysis Gate: verifies orderbook walls, consults DuckDB trade_lessons, and sends a 3-bullet summary to Telegram."""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from database.db_manager import DatabaseManager
from engine.mistake_learner import MistakeLearner
from engine.openrouter_pool import OpenRouterPool
from engine.schemas import ReflexDecisionSchema

logger = logging.getLogger(__name__)


class PreTradeAnalyzer:
    """Executes final deep verification (Orderbook Wall + Mistake Learner + 3-bullet Telegram briefing) before firing an order."""

    def __init__(
        self,
        db: DatabaseManager,
        exchange: Optional[Any] = None,
        openrouter_pool: Optional[OpenRouterPool] = None,
        telegram: Optional[Any] = None,
        mistake_learner: Optional[MistakeLearner] = None,
    ) -> None:
        self.db = db
        self.exchange = exchange
        self.openrouter_pool = openrouter_pool
        self.telegram = telegram
        self.mistake_learner = mistake_learner or MistakeLearner(
            db=db, openrouter_pool=openrouter_pool, telegram=telegram
        )

    async def analyze_before_execution(
        self,
        decision: ReflexDecisionSchema,
        trading_mode: str = "SPOT",
        position_usd: float = 11.50,
        leverage: int = 1,
        min_confidence: float = 0.68,
        send_telegram: bool = True,
    ) -> Dict[str, Any]:
        """
        1. Check Orderbook Imbalance & top-of-book sell wall directly above entry.
        2. Consult `trade_lessons` in DuckDB to block repeat mistakes or apply confidence penalty.
        3. Generate a concise 3-bullet Pre-Trade Analysis Summary and send to Telegram.
        """
        snap = self.db.get_latest_state_snapshot(decision.symbol) or {}
        entry_rsi = float(snap.get("rsi_14", 50.0))
        ob_imbalance = float(snap.get("orderbook_imbalance", 0.0))
        sell_wall_ratio = 1.0

        # Live orderbook check if exchange supports fetch_order_book
        if self.exchange is not None and hasattr(self.exchange, "fetch_order_book"):
            try:
                ob = await self.exchange.fetch_order_book(decision.symbol, limit=20)
                bids = ob.get("bids") or []
                asks = ob.get("asks") or []
                if bids and asks:
                    bid_vol_15 = sum(float(b[1]) for b in bids[:15] if len(b) >= 2)
                    ask_vol_15 = sum(float(a[1]) for a in asks[:15] if len(a) >= 2)
                    total_15 = bid_vol_15 + ask_vol_15
                    if total_15 > 0:
                        ob_imbalance = (bid_vol_15 - ask_vol_15) / total_15
                    if bid_vol_15 > 0:
                        sell_wall_ratio = ask_vol_15 / bid_vol_15
            except Exception as exc:
                logger.debug("PreTradeAnalyzer live orderbook check skipped: %s", exc)

        # (a) Reject if there is an extreme sell wall directly above entry
        if ob_imbalance < -0.68 or sell_wall_ratio > 5.5:
            reason = (
                f"Massive sell wall detected above entry on {decision.symbol} "
                f"(OB Imbalance={ob_imbalance:+.2f}, Ask/Bid Ratio={sell_wall_ratio:.2f}x)"
            )
            logger.info("Pre-Trade Gate blocked %s: %s", decision.symbol, reason)
            return {
                "approved": False,
                "reason": reason,
                "effective_confidence": decision.direction_confidence,
                "orderbook_imbalance": ob_imbalance,
            }

        # (b) Consult trade_lessons table in DuckDB
        lesson_eval = self.mistake_learner.evaluate_setup_against_lessons(
            symbol=decision.symbol,
            regime=decision.regime,
            entry_rsi=entry_rsi,
            orderbook_imbalance=ob_imbalance,
        )
        if not lesson_eval["allowed"]:
            logger.info("Pre-Trade Gate blocked %s via MistakeLearner: %s", decision.symbol, lesson_eval["reason"])
            return {
                "approved": False,
                "reason": lesson_eval["reason"],
                "effective_confidence": decision.direction_confidence,
                "orderbook_imbalance": ob_imbalance,
            }

        penalty = float(lesson_eval.get("confidence_penalty", 0.0))
        effective_confidence = round(float(decision.direction_confidence) - penalty, 4)
        if effective_confidence < min_confidence:
            reason = (
                f"Effective confidence {effective_confidence:.2f} (after -{penalty:.2f} lesson penalty) "
                f"is below threshold {min_confidence:.2f}"
            )
            logger.info("Pre-Trade Gate blocked %s: %s", decision.symbol, reason)
            return {
                "approved": False,
                "reason": reason,
                "effective_confidence": effective_confidence,
                "orderbook_imbalance": ob_imbalance,
            }

        # (c) Generate concise 3-bullet Pre-Trade Analysis Summary
        mode_label = f"FUTURES {leverage}x ISOLATED" if trading_mode.upper().startswith("FUTURE") else "SPOT"
        deterministic_bullets = (
            f"• *Teknik Kurulum:* `{decision.symbol}` `{decision.regime.upper()}` rejiminde "
            f"(RSI: `{entry_rsi:.1f}`, Q=`{decision.setup_quality}`, R:R=`1:{decision.expected_reward_risk_ratio:.1f}`).\n"
            f"• *Emir Defteri:* Kademe dengesi `{ob_imbalance:+.2f}` (Satış/Alış oranı `{sell_wall_ratio:.2f}x`), "
            f"giriş üstünde devasa satış duvarı yok.\n"
            f"• *Risk & Hafıza Onayı:* `{mode_label}` büyüklük `${position_usd:.2f}`, "
            f"net güven `%{effective_confidence * 100:.1f}` (Ceza: `-%{penalty * 100:.0f}`), geçmiş hata filtresi temiz."
        )

        summary_text = deterministic_bullets
        if self.openrouter_pool is not None:
            prompt = (
                f"Create a concise 3-bullet Turkish Pre-Trade Analysis Summary (each line starting with '• ') "
                f"for this approved crypto trade:\n"
                f"Symbol: {decision.symbol}, Mode: {mode_label}, Size: ${position_usd:.2f}, "
                f"Regime: {decision.regime}, RSI: {entry_rsi:.1f}, Confidence: {effective_confidence*100:.1f}%, "
                f"Orderbook Imbalance: {ob_imbalance:+.2f}, Rationale: {decision.rationale}"
            )
            try:
                res = await self.openrouter_pool.complete_with_fallback(
                    prompt=prompt,
                    deterministic_fallback=deterministic_bullets,
                    max_tokens=220,
                )
                content = str(res.get("content") or "").strip()
                if content and "•" in content:
                    summary_text = content
            except Exception as exc:
                logger.debug("PreTradeAnalyzer LLM summary fallback used: %s", exc)

        telegram_msg = (
            f"🔍 *İŞLEM ÖNCESİ DERİN ANALİZ ({mode_label})*\n\n"
            f"{summary_text}"
        )
        if self.telegram is not None and send_telegram:
            await self.telegram.send_message(telegram_msg)

        return {
            "approved": True,
            "reason": "Pre-Trade Gate passed",
            "effective_confidence": effective_confidence,
            "orderbook_imbalance": ob_imbalance,
            "summary": summary_text,
            "telegram_message": telegram_msg,
        }
