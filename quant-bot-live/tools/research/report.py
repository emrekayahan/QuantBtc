"""K1 Research Report Generator: regime/side/hour breakdown, costs, drawdown, G1 gate."""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, List

from tools.research.backtest import BacktestResult
from tools.research.gates import GateResult
from tools.research.stats import cluster_bootstrap_ci


def format_research_report(
    strategy_id: str,
    result: BacktestResult,
    gate: GateResult,
    seed: int = 42,
) -> str:
    """Render reproducible research report in Markdown."""
    trades = result.trades
    boot = cluster_bootstrap_ci(trades, n_resamples=2000, seed=seed)
    wins = sum(1 for t in trades if t["r_net"] > 0)
    win_rate = (wins / len(trades) * 100.0) if trades else 0.0

    by_side: Dict[str, List[float]] = defaultdict(list)
    for t in trades:
        by_side[t["side"]].append(t["r_net"])

    lines = [
        f"# Research Report: `{strategy_id}` (seed={seed})",
        "",
        f"- **G1 Gate:** `{'PASS' if gate.passed else 'FAIL'}`",
        f"- **Trades (filled):** {len(trades)} | **Missed (unfilled maker):** {len(result.missed_signals)}",
        f"- **WinRate (net > 0):** {win_rate:.1f}%",
        f"- **Mean Net R:** {boot['mean']:+.4f} R | **90% CI:** [{boot['ci_low']:+.4f}, {boot['ci_high']:+.4f}] | **80% LCB:** {boot['lcb_80']:+.4f}",
        f"- **Max Drawdown:** {result.max_drawdown_pct:.2f}% | **End Equity:** {result.equity_curve[-1]:.2f}",
        "",
        "## Cost Breakdown (avg per trade in R)",
        f"- Fees: {result.cost_breakdown['avg_fee_r']:.4f} R",
        f"- Slippage: {result.cost_breakdown['avg_slippage_r']:.4f} R",
        f"- Funding: {result.cost_breakdown['avg_funding_r']:.4f} R",
        f"- Total Cost: {result.cost_breakdown['avg_total_cost_r']:.4f} R",
        "",
        "## By Side",
    ]
    for side, vals in sorted(by_side.items()):
        lines.append(f"- `{side}`: n={len(vals)}, mean_net_r={sum(vals)/len(vals):+.4f} R")

    lines.append("")
    lines.append("## By Regime")
    for reg, st in sorted(result.regime_stats.items()):
        lines.append(f"- `{reg}`: n={st['n']}, mean_net_r={st['mean_net_r']:+.4f} R")

    if gate.reasons:
        lines.append("")
        lines.append("## G1 Failure Reasons")
        for r in gate.reasons:
            lines.append(f"- {r}")

    return "\n".join(lines)
