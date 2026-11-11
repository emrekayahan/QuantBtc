"""CLI tool to report signal outcome statistics grouped by regime, strategy, and confidence."""

from __future__ import annotations

import argparse
from typing import Any, Dict, List

from database.db_manager import DatabaseManager


def generate_signal_report(db_path: str = "") -> str:
    db = DatabaseManager(db_path=db_path) if db_path else DatabaseManager()
    try:
        with db._lock:
            # Overall summary
            overall = db._conn.execute("""
                SELECT 
                    count(*) as total,
                    sum(CASE WHEN result = 'TP' THEN 1 ELSE 0 END) as tp_count,
                    sum(CASE WHEN result = 'SL' THEN 1 ELSE 0 END) as sl_count,
                    sum(CASE WHEN result = 'TIMEOUT' THEN 1 ELSE 0 END) as timeout_count,
                    avg(r_net) as avg_r_net,
                    avg(r_gross) as avg_r_gross
                FROM signal_outcomes
            """).fetchone()

            # Group by regime
            by_regime = db._conn.execute("""
                SELECT 
                    regime,
                    count(*) as n,
                    round(sum(CASE WHEN result = 'TP' THEN 1.0 ELSE 0.0 END) / NULLIF(count(*), 0) * 100, 1) as win_rate_pct,
                    round(avg(r_net), 3) as avg_r_net
                FROM signal_outcomes
                GROUP BY regime
                ORDER BY n DESC
            """).fetchall()

            # Group by strategy_id
            by_strat = db._conn.execute("""
                SELECT 
                    strategy_id,
                    count(*) as n,
                    round(sum(CASE WHEN result = 'TP' THEN 1.0 ELSE 0.0 END) / NULLIF(count(*), 0) * 100, 1) as win_rate_pct,
                    round(avg(r_net), 3) as avg_r_net
                FROM signal_outcomes
                GROUP BY strategy_id
                ORDER BY n DESC
            """).fetchall()

        total = overall[0] or 0
        tp = overall[1] or 0
        sl = overall[2] or 0
        timeout = overall[3] or 0
        avg_r = overall[4] or 0.0

        win_rate = (tp / (tp + sl) * 100.0) if (tp + sl) > 0 else 0.0

        lines = [
            "=" * 60,
            "SIGNAL OUTCOMES PERFORMANCE REPORT",
            "=" * 60,
            f"Total Resolved Signals : {total}",
            f"TP: {tp} | SL: {sl} | Timeout: {timeout}",
            f"Win Rate (TP / (TP+SL)): {win_rate:.1f}%",
            f"Average Net R          : {avg_r:+.4f}",
            "-" * 60,
            "PERFORMANCE BY REGIME:",
        ]
        for r in by_regime:
            lines.append(f"  - {r[0]:<20} | N={r[1]:<4} | WinRate={r[2]}% | AvgNetR={r[3]:+.3f}")

        lines.append("-" * 60)
        lines.append("PERFORMANCE BY STRATEGY:")
        for s in by_strat:
            lines.append(f"  - {s[0]:<20} | N={s[1]:<4} | WinRate={s[2]}% | AvgNetR={s[3]:+.3f}")
        lines.append("=" * 60)

        report_str = "\n".join(lines)
        return report_str
    finally:
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate signal outcomes performance report.")
    parser.add_argument("--db", default="", help="Path to database (optional)")
    args = parser.parse_args()
    print(generate_signal_report(args.db))
