"""CLI tool to report signal outcome statistics grouped by regime, strategy, and confidence."""

from __future__ import annotations

import argparse
import os
import shutil
import tempfile
import time
from typing import Any, Dict, List, Optional

import duckdb
from config.settings import settings
from database.db_manager import DatabaseManager


def parse_since(since_str: str) -> int:
    """Parse string like '3d', '7d', '24h', '30m' into epoch ms timestamp."""
    now_ms = int(time.time() * 1000)
    s = since_str.strip().lower()
    if s.endswith("d"):
        days = float(s[:-1])
        return int(now_ms - days * 86400 * 1000)
    elif s.endswith("h"):
        hours = float(s[:-1])
        return int(now_ms - hours * 3600 * 1000)
    elif s.endswith("m"):
        mins = float(s[:-1])
        return int(now_ms - mins * 60 * 1000)
    return int(s) if s.isdigit() else int(now_ms - 3 * 86400 * 1000)


def get_db_connection(custom_path: str = "") -> tuple[duckdb.DuckDBPyConnection, Optional[str]]:
    """Connect to DuckDB safely. If file is locked by live engine, copy to /tmp and query copy."""
    target_path = custom_path or getattr(settings, "effective_db_path", "data/quant_bot_live.db")
    tmp_path = None
    try:
        conn = duckdb.connect(target_path, read_only=True)
        return conn, None
    except Exception:
        # Conflicting lock held by live trading process -> query snapshot copy
        tmp_fd, tmp_path = tempfile.mkstemp(prefix="qbot_report_", suffix=".db")
        os.close(tmp_fd)
        shutil.copy2(target_path, tmp_path)
        wal_path = target_path + ".wal"
        if os.path.exists(wal_path):
            try:
                shutil.copy2(wal_path, tmp_path + ".wal")
            except Exception:
                pass
        conn = duckdb.connect(tmp_path, read_only=True)
        return conn, tmp_path


def generate_signal_report(since_str: str = "3d", db_path: str = "") -> str:
    cutoff_ms = parse_since(since_str)
    conn, tmp_copy = get_db_connection(db_path)

    try:
        # 1. Overall Signal Outcomes Summary (exclude NOT_FILLED and MISSED from WinRate & mean R)
        overall = conn.execute(f"""
            SELECT 
                count(*) as total,
                sum(CASE WHEN result = 'TP' THEN 1 ELSE 0 END) as tp_count,
                sum(CASE WHEN result = 'SL' THEN 1 ELSE 0 END) as sl_count,
                sum(CASE WHEN result = 'TIMEOUT' THEN 1 ELSE 0 END) as timeout_count,
                avg(CASE WHEN result NOT IN ('NOT_FILLED', 'MISSED') THEN r_net END) as avg_r_net,
                avg(CASE WHEN result NOT IN ('NOT_FILLED', 'MISSED') THEN r_gross END) as avg_r_gross
            FROM signal_outcomes
            WHERE resolved_at_ms >= {cutoff_ms}
        """).fetchone()

        total = overall[0] or 0
        tp = overall[1] or 0
        sl = overall[2] or 0
        timeout = overall[3] or 0
        avg_r_net = overall[4] or 0.0
        avg_r_gross = overall[5] or 0.0
        win_rate = (tp / (tp + sl) * 100.0) if (tp + sl) > 0 else 0.0

        # 2. Performance by Regime
        by_regime = conn.execute(f"""
            SELECT 
                regime,
                count(*) as n,
                sum(CASE WHEN result = 'TP' THEN 1 ELSE 0 END) as tp,
                sum(CASE WHEN result = 'SL' THEN 1 ELSE 0 END) as sl,
                sum(CASE WHEN result = 'TIMEOUT' THEN 1 ELSE 0 END) as to_cnt,
                round(avg(r_net), 4) as avg_r_net
            FROM signal_outcomes
            WHERE resolved_at_ms >= {cutoff_ms}
            GROUP BY regime
            ORDER BY n DESC
        """).fetchall()

        # 3. Performance by Confidence Bucket (joined with reflex_decisions)
        by_confidence = conn.execute(f"""
            SELECT 
                CASE 
                    WHEN rd.direction_confidence >= 0.80 THEN '0.80+'
                    WHEN rd.direction_confidence >= 0.70 THEN '0.70 - 0.80'
                    ELSE '0.60 - 0.70'
                END as conf_bucket,
                count(*) as n,
                sum(CASE WHEN so.result = 'TP' THEN 1 ELSE 0 END) as tp,
                sum(CASE WHEN so.result = 'SL' THEN 1 ELSE 0 END) as sl,
                round(avg(so.r_net), 4) as avg_r_net
            FROM signal_outcomes so
            LEFT JOIN reflex_decisions rd ON so.decision_id = rd.id
            WHERE so.resolved_at_ms >= {cutoff_ms}
            GROUP BY conf_bucket
            ORDER BY conf_bucket DESC
        """).fetchall()

        # 4. Performance by Setup Quality
        by_quality = conn.execute(f"""
            SELECT 
                COALESCE(rd.setup_quality, 0) as quality,
                count(*) as n,
                sum(CASE WHEN so.result = 'TP' THEN 1 ELSE 0 END) as tp,
                sum(CASE WHEN so.result = 'SL' THEN 1 ELSE 0 END) as sl,
                round(avg(so.r_net), 4) as avg_r_net
            FROM signal_outcomes so
            LEFT JOIN reflex_decisions rd ON so.decision_id = rd.id
            WHERE so.resolved_at_ms >= {cutoff_ms}
            GROUP BY quality
            ORDER BY quality DESC
        """).fetchall()

        # 5. Live Closed Positions & Realized PnL (positions table)
        live_positions = conn.execute(f"""
            SELECT 
                symbol,
                market_type,
                entry_price,
                current_price,
                realized_pnl_usd,
                close_reason,
                opened_at_ms,
                closed_at_ms
            FROM positions
            WHERE status = 'CLOSED' AND (closed_at_ms >= {cutoff_ms} OR closed_at_ms IS NULL)
            ORDER BY closed_at_ms DESC
        """).fetchall()

        # Positions aggregations
        pos_total = len(live_positions)
        pos_wins = sum(1 for p in live_positions if (p[4] or 0.0) > 0)
        pos_losses = sum(1 for p in live_positions if (p[4] or 0.0) < 0)
        pos_pnl_total = sum(float(p[4] or 0.0) for p in live_positions)

        # Build formatted report
        lines = [
            "=" * 74,
            f"🎯 QUANTBOT SIGNAL OUTCOMES & PnL REPORT (Lookback: {since_str.upper()})",
            "=" * 74,
            f"• Toplam Sonuçlanan Sinyal : {total}",
            f"• Sonuç Kırılımı           : TP: {tp} | SL: {sl} | Timeout: {timeout}",
            f"• Kar Alma Oranı (Win Rate): {win_rate:.1f}% (TP / [TP + SL])",
            f"• Ortalama Net R (R_net)   : {avg_r_net:+.4f} R",
            f"• Ortalama Brüt R (R_gross): {avg_r_gross:+.4f} R",
            "-" * 74,
            "📊 PİYASA REJİMİNE GÖRE SİNYAL DAĞILIMI:",
        ]

        if by_regime:
            for r in by_regime:
                r_tp, r_sl = r[2], r[3]
                r_wr = (r_tp / (r_tp + r_sl) * 100.0) if (r_tp + r_sl) > 0 else 0.0
                lines.append(f"  • {r[0]:<16} | N={r[1]:<3} | TP={r_tp:<2} SL={r_sl:<2} TO={r[4]:<2} | WinRate={r_wr:5.1f}% | AvgNetR={r[5]:+.4f}R")
        else:
            lines.append("  (Bu zaman aralığında sonuçlanan rejim sinyali yok)")

        lines.append("-" * 74)
        lines.append("🎯 GÜVEN DERECESİNE GÖRE PERFORMANS (CONFIDENCE BUCKET):")
        if by_confidence:
            for c in by_confidence:
                c_tp, c_sl = c[2], c[3]
                c_wr = (c_tp / (c_tp + c_sl) * 100.0) if (c_tp + c_sl) > 0 else 0.0
                lines.append(f"  • Güven: {c[0]:<12} | N={c[1]:<3} | TP={c_tp:<2} SL={c_sl:<2} | WinRate={c_wr:5.1f}% | AvgNetR={c[4]:+.4f}R")
        else:
            lines.append("  (Veri yok)")

        lines.append("-" * 74)
        lines.append("⭐ KURULUM KALİTESİNE GÖRE (SETUP QUALITY):")
        if by_quality:
            for q in by_quality:
                q_tp, q_sl = q[2], q[3]
                q_wr = (q_tp / (q_tp + q_sl) * 100.0) if (q_tp + q_sl) > 0 else 0.0
                lines.append(f"  • Quality: Q{q[0]:<1}       | N={q[1]:<3} | TP={q_tp:<2} SL={q_sl:<2} | WinRate={q_wr:5.1f}% | AvgNetR={q[4]:+.4f}R")
        else:
            lines.append("  (Veri yok)")

        lines.append("=" * 74)
        lines.append("💼 CANLI GERÇEKLEŞEN İŞLEMLER (EXECUTED POSITIONS PnL):")
        lines.append(f"• Kapanan İşlem Sayısı: {pos_total} | Kazanç: {pos_wins} | Kayıp: {pos_losses}")
        lines.append(f"• Toplam Realized PnL : ${pos_pnl_total:+.4f} USD")
        lines.append("-" * 74)
        lines.append(f"{'Sembol':<12} {'Tür':<8} {'Giriş':<10} {'Çıkış':<10} {'PnL ($)':<10} {'Neden'}")
        lines.append("-" * 74)

        if live_positions:
            for p in live_positions[:15]:
                sym = p[0]
                m_type = str(p[1] or 'spot').upper()
                entry = f"${float(p[2] or 0):,.2f}"
                exit_p = f"${float(p[3] or 0):,.2f}"
                pnl_usd = f"${float(p[4] or 0):+,.4f}"
                reason = str(p[5] or '-')
                lines.append(f"{sym:<12} {m_type:<8} {entry:<10} {exit_p:<10} {pnl_usd:<10} {reason}")
        else:
            lines.append("  (Bu zaman aralığında kapanan canlı işlem yok)")

        lines.append("=" * 74)
        return "\n".join(lines)

    finally:
        conn.close()
        if tmp_copy and os.path.exists(tmp_copy):
            try:
                os.remove(tmp_copy)
                if os.path.exists(tmp_copy + ".wal"):
                    os.remove(tmp_copy + ".wal")
            except Exception:
                pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate signal outcomes performance report.")
    parser.add_argument("--since", default="3d", help="Lookback window (e.g. '3d', '7d', '24h', '12h')")
    parser.add_argument("--db", default="", help="Path to database (optional)")
    args = parser.parse_args()
    print(generate_signal_report(since_str=args.since, db_path=args.db))
