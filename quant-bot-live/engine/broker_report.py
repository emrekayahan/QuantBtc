"""K11: BrokerReporter & decision_log — structured decision logging and daily Telegram broker report."""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from database.db_manager import DatabaseManager


class BrokerReporter:
    """Writes structured decision_log entries and formats the 21:00 UTC daily broker report."""

    def __init__(self, db: DatabaseManager) -> None:
        self.db = db

    def log_decision(
        self,
        decision_id: str,
        strategy_id: str,
        symbol: str,
        side: str,
        risk_pct: float,
        leverage: int,
        stop_dist_pct: float,
        reason: str,
    ) -> None:
        now_ms = int(time.time() * 1000)
        with self.db._lock:
            self.db._conn.execute(
                """
                INSERT INTO decision_log (
                    decision_id, strategy_id, symbol, side, risk_pct, leverage, stop_dist_pct, reason, ts_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (decision_id) DO NOTHING
                """,
                [
                    str(decision_id),
                    str(strategy_id),
                    str(symbol),
                    str(side),
                    float(risk_pct),
                    int(leverage),
                    float(stop_dist_pct),
                    str(reason),
                    now_ms,
                ],
            )

    def format_daily_broker_report(
        self,
        total_equity: float,
        futures_equity: float,
        spot_equity: float,
        daily_net_pnl: float,
        weekly_net_pnl: float,
    ) -> str:
        cutoff_ms = int(time.time() * 1000) - 86_400_000
        open_pos = self.db.get_open_positions(include_unprotected=True)
        strategies = self.db._conn.execute(
            "SELECT strategy_id, state, g1_passed FROM strategy_lifecycle ORDER BY strategy_id"
        ).fetchall()
        decisions = self.db._conn.execute(
            """
            SELECT strategy_id, symbol, side, risk_pct, leverage, reason
            FROM decision_log
            WHERE ts_ms >= ?
            ORDER BY ts_ms DESC
            LIMIT 10
            """,
            [cutoff_ms],
        ).fetchall()

        state = self.db.get_all_system_state()
        acc_state = state.get("account_state", "NORMAL")

        lines = [
            "📊 *Günlük Broker Raporu (21:00 UTC)*",
            f"• Toplam Bakiye: `${total_equity:.2f}` (Futures: `${futures_equity:.2f}`, Spot: `${spot_equity:.2f}`)",
            f"• Net PnL: Günlük `{daily_net_pnl:+.2f}$` | Haftalık `{weekly_net_pnl:+.2f}$`",
            f"• Hesap Durumu: `{acc_state}` | SafeMode: `{self.db.is_safe_mode()}`",
            "",
            f"*Açık Pozisyonlar ({len(open_pos)}):*",
        ]
        if not open_pos:
            lines.append("• Yok")
        else:
            for p in open_pos:
                lines.append(f"• `{p.get('symbol')}` {p.get('side')} ({p.get('leverage', 1)}x) SL=`{p.get('sl_exchange_order_id') or 'NONE'}`")

        lines.append("")
        lines.append("*Strateji Tablosu:*")
        if not strategies:
            lines.append("• Kayıtlı strateji yok (`shadow`)")
        else:
            for s in strategies:
                lines.append(f"• `{s[0]}`: `{s[1]}` (G1=`{s[2]}`)")

        lines.append("")
        lines.append("*Bugün Botun Kararları:*")
        if not decisions:
            lines.append("• İşlem kararı yok")
        else:
            for d in decisions:
                lines.append(f"• `{d[0]}` `{d[1]}` {d[2]} | risk=`{d[3]:.2f}%` `{d[4]}x` | {d[5]}")

        return "\n".join(lines)
