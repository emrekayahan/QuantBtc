"""R18 Daily Executive Digest Generator: produces single-screen broker summary for Telegram/logs."""

from __future__ import annotations

import datetime as dt
import logging
import time
from typing import Any, Dict, List, Optional

from config.settings import Settings, settings
from database.db_manager import DatabaseManager

logger = logging.getLogger(__name__)


class ExecutiveDigest:
    """Generates daily broker summary at 21:00 UTC."""

    def __init__(
        self,
        db: DatabaseManager,
        custom_settings: Optional[Settings] = None,
    ) -> None:
        self.db = db
        self.cfg = custom_settings or settings

    def generate_digest_markdown(self) -> str:
        now_ms = int(time.time() * 1000)
        since_24h_ms = now_ms - 86_400_000

        total_equity = float(self.db.get_state("total_equity_usd", "0.0") or 0.0)
        spot_free = float(self.db.get_state("spot_free_usdt", "0.0") or 0.0)
        futures_free = float(self.db.get_state("futures_free_usdt", "0.0") or 0.0)
        daily_pnl = self.db.get_daily_realized_pnl()
        drawdown_pct = float(self.db.get_state("current_drawdown_pct", "0.0") or 0.0)
        autonomy_lvl = self.db.get_autonomy_level()
        safe_mode = self.db.is_safe_mode()
        halted = self.db.is_system_halted()

        # Open positions summary
        open_pos = self.db.get_open_positions(include_unprotected=True)
        pos_lines = []
        for p in open_pos:
            m_type = p.get("market_type", "spot").upper()
            lev = p.get("leverage", 1)
            sl_cov = "SECURED" if p.get("sl_exchange_order_id") else "UNPROTECTED"
            pos_lines.append(f"• `{p['symbol']}` ({m_type}{f' {lev}x' if lev > 1 else ''}): ${float(p.get('cost_usd', 0)):.1f} | SL: {sl_cov}")

        pos_str = "\n".join(pos_lines) if pos_lines else "• Açık pozisyon yok."

        # Learning / Lessons summary
        recent_lessons = self.db.get_trade_lessons(limit=3)
        lesson_lines = []
        for l in recent_lessons:
            lesson_lines.append(f"• `{l['symbol']}`: {l['failure_reason'][:50]}... (-%{int(float(l['confidence_penalty'])*100)})")
        lesson_str = "\n".join(lesson_lines) if lesson_lines else "• Son 24 saatte yeni hata dersi kaydedilmedi."

        # Health status
        health_status = "🔴 HALTED" if halted else ("🟡 SAFE MODE" if safe_mode else "🟢 HEALTHY")

        date_str = dt.datetime.now(dt.timezone.utc).strftime("%d.%m.%Y")
        report = (
            f"📊 *GÜNLÜK BROKER RAPORU ({date_str})*\n\n"
            f"💼 *Bakiye ve Sermaye:*\n"
            f"• Toplam Özkaynak: `${total_equity:,.2f}`\n"
            f"• Spot Serbest: `${spot_free:,.2f}` | Futures: `${futures_free:,.2f}`\n"
            f"• 24s Net K/Z: `${daily_pnl:+,.2f}`\n"
            f"• Mevcut Drawdown: `%{drawdown_pct:.1f}`\n\n"
            f"📈 *Açık Pozisyonlar ({len(open_pos)}):*\n{pos_str}\n\n"
            f"🧠 *Öğrenme ve Kalibrasyon Özeti:*\n{lesson_str}\n\n"
            f"🛡️ *Sistem ve Otonomi:*\n"
            f"• Otonomi Seviyesi: `L{autonomy_lvl}`\n"
            f"• Sistem Durumu: {health_status}\n"
        )
        return report
