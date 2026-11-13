"""Section 6 Proof Gates (G0-G3 & Demotion): mandatory, non-bypassable in code."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from config.settings import Settings, settings
from tools.research.stats import cluster_bootstrap_ci, deflated_sharpe_ratio


@dataclass
class GateResult:
    gate: str
    passed: bool
    reasons: List[str] = field(default_factory=list)
    metrics: Dict[str, Any] = field(default_factory=dict)


def evaluate_g1_gate(
    trades: Sequence[Dict[str, Any]],
    regime_stats: Dict[str, Dict[str, Any]],
    trial_count: int = 1,
    custom_settings: Optional[Settings] = None,
    alpha: float = 0.10,
    seed: int = 42,
) -> GateResult:
    """
    G1 (shadow -> learn):
    1. n >= G1_MIN_TRADES (150)
    2. mean_net_r > 0
    3. lcb_80 >= G1_LCB_MIN_R (-0.05)
    4. >= 3 distinct market regimes with non-negative mean_net_r
    5. Multiple-testing check: Bonferroni (p_val <= alpha / K) OR Deflated Sharpe > 0
    """
    cfg = custom_settings or settings
    min_trades = int(cfg.G1_MIN_TRADES)
    min_lcb = float(cfg.G1_LCB_MIN_R)

    n = len(trades)
    boot = cluster_bootstrap_ci(trades, n_resamples=5000, seed=seed)
    mean_net_r = float(boot["mean"])
    lcb_80 = float(boot["lcb_80"])
    p_val = float(boot["p_value_one_sided"])
    dsr = deflated_sharpe_ratio([float(t.get("r_net", 0.0)) for t in trades], n_trials=max(1, trial_count))

    non_neg_regimes = [
        name for name, st in regime_stats.items()
        if int(st.get("n", 0)) > 0 and float(st.get("mean_net_r", -1.0)) >= 0.0
    ]

    bonferroni_thresh = alpha / max(1, int(trial_count))
    multi_test_ok = (p_val <= bonferroni_thresh) or (dsr > 0.0)

    reasons: List[str] = []
    if n < min_trades:
        reasons.append(f"n_trades={n} < G1_MIN_TRADES ({min_trades})")
    if mean_net_r <= 0.0:
        reasons.append(f"mean_net_r={mean_net_r:.4f} <= 0.0")
    if lcb_80 < min_lcb:
        reasons.append(f"lcb_80={lcb_80:.4f} < G1_LCB_MIN_R ({min_lcb})")
    if len(non_neg_regimes) < 3:
        reasons.append(f"non_negative_regimes={len(non_neg_regimes)} < 3 ({non_neg_regimes})")
    if not multi_test_ok:
        reasons.append(f"multiple_testing failed (p={p_val:.4f} > {bonferroni_thresh:.4f} and DSR={dsr:.4f} <= 0)")

    return GateResult(
        gate="G1",
        passed=(len(reasons) == 0),
        reasons=reasons,
        metrics={
            "n_trades": n,
            "mean_net_r": mean_net_r,
            "lcb_80": lcb_80,
            "p_value_one_sided": p_val,
            "deflated_sharpe": dsr,
            "non_negative_regimes": len(non_neg_regimes),
            "trial_count": trial_count,
        },
    )


def evaluate_g2_gate(
    n_live_trades: int,
    p_mu_pos: float,
    mu_mean: float,
    max_drawdown_pct: float,
    safety_violations: int = 0,
) -> GateResult:
    """G2 (learn -> exploit_low): n>=60, P(mu>0)>=0.80, mu_mean>0, dd<10%, 0 SL/liq violations."""
    reasons: List[str] = []
    if n_live_trades < 60:
        reasons.append(f"n_live_trades={n_live_trades} < 60")
    if p_mu_pos < 0.80:
        reasons.append(f"p_mu_pos={p_mu_pos:.3f} < 0.80")
    if mu_mean <= 0.0:
        reasons.append(f"mu_mean={mu_mean:.4f} <= 0")
    if max_drawdown_pct >= 10.0:
        reasons.append(f"max_drawdown_pct={max_drawdown_pct:.2f}% >= 10.0%")
    if safety_violations > 0:
        reasons.append(f"safety_violations={safety_violations} > 0")
    return GateResult(gate="G2", passed=(len(reasons) == 0), reasons=reasons)


def evaluate_g3_gate(
    n_total_trades: int,
    p_mu_pos: float,
    mu_lcb: float,
    reconcile_violations: int = 0,
) -> GateResult:
    """G3 (exploit_low -> exploit_high): n>=150, P(mu>0)>=0.95, mu_lcb>0.05R, 0 reconcile violations."""
    reasons: List[str] = []
    if n_total_trades < 150:
        reasons.append(f"n_total_trades={n_total_trades} < 150")
    if p_mu_pos < 0.95:
        reasons.append(f"p_mu_pos={p_mu_pos:.3f} < 0.95")
    if mu_lcb <= 0.05:
        reasons.append(f"mu_lcb={mu_lcb:.4f} <= 0.05")
    if reconcile_violations > 0:
        reasons.append(f"reconcile_violations={reconcile_violations} > 0")
    return GateResult(gate="G3", passed=(len(reasons) == 0), reasons=reasons)


def should_demote_to_suspended(rolling_30_net_r: float, p_mu_pos: float, n_trades: int) -> bool:
    """Demote to suspended if rolling-30 net R < -0.15 or P(mu>0) < 0.30 with n >= 30."""
    if n_trades >= 30 and (rolling_30_net_r < -0.15 or p_mu_pos < 0.30):
        return True
    return False
