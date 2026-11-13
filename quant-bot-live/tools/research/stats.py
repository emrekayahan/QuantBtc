"""K1 Statistical validation: cluster bootstrap, purged walk-forward, and Deflated Sharpe Ratio."""
from __future__ import annotations

import math
from collections import defaultdict
from statistics import NormalDist
from typing import Any, Dict, List, Sequence, Tuple

import numpy as np


def cluster_bootstrap_ci(
    trades: Sequence[Dict[str, Any]],
    n_resamples: int = 10000,
    ci_level: float = 0.90,
    seed: int = 42,
) -> Dict[str, float]:
    """
    Compute cluster-bootstrapped mean net R, 90% CI, 80% lower confidence bound (LCB),
    and one-sided p-value (H0: mean <= 0) by resampling (day, symbol) clusters.
    """
    if not trades:
        return {
            "n_trades": 0,
            "n_clusters": 0,
            "mean": 0.0,
            "std": 0.0,
            "ci_low": 0.0,
            "ci_high": 0.0,
            "lcb_80": 0.0,
            "p_value_one_sided": 1.0,
        }

    clusters: Dict[str, List[float]] = defaultdict(list)
    for idx, t in enumerate(trades):
        key = str(t.get("cluster_key") or t.get("date_utc") or f"c_{idx}")
        clusters[key].append(float(t.get("r_net", 0.0)))

    cluster_sums = np.array([sum(vals) for vals in clusters.values()], dtype=np.float64)
    cluster_counts = np.array([len(vals) for vals in clusters.values()], dtype=np.float64)
    n_clusters = len(cluster_sums)
    total_trades = int(cluster_counts.sum())
    sample_mean = float(cluster_sums.sum() / max(1.0, cluster_counts.sum()))

    if n_clusters == 1:
        return {
            "n_trades": total_trades,
            "n_clusters": 1,
            "mean": round(sample_mean, 6),
            "std": 0.0,
            "ci_low": round(sample_mean, 6),
            "ci_high": round(sample_mean, 6),
            "lcb_80": round(sample_mean, 6),
            "p_value_one_sided": 0.0 if sample_mean > 0 else 1.0,
        }

    rng = np.random.default_rng(seed)
    idx_matrix = rng.integers(0, n_clusters, size=(n_resamples, n_clusters))
    boot_sums = cluster_sums[idx_matrix].sum(axis=1)
    boot_counts = np.maximum(1.0, cluster_counts[idx_matrix].sum(axis=1))
    boot_means = boot_sums / boot_counts

    alpha = (1.0 - ci_level) / 2.0
    ci_low = float(np.quantile(boot_means, alpha))
    ci_high = float(np.quantile(boot_means, 1.0 - alpha))
    lcb_80 = float(np.quantile(boot_means, 0.20))
    p_val = float(np.mean(boot_means <= 0.0))

    return {
        "n_trades": total_trades,
        "n_clusters": n_clusters,
        "mean": round(sample_mean, 6),
        "std": round(float(np.std(boot_means, ddof=1)), 6),
        "ci_low": round(ci_low, 6),
        "ci_high": round(ci_high, 6),
        "lcb_80": round(lcb_80, 6),
        "p_value_one_sided": round(p_val, 6),
    }


def purged_walk_forward_splits(
    total_bars: int,
    train_bars: int = 5760,   # 60 days * 96 15m bars/day
    test_bars: int = 1440,    # 15 days * 96 15m bars/day
    embargo_bars: int = 32,   # max hold horizon gap
) -> List[Tuple[range, range]]:
    """Generate purged & embargoed walk-forward (train_range, test_range) splits."""
    splits: List[Tuple[range, range]] = []
    start = 0
    while True:
        train_end = start + train_bars
        test_start = train_end + embargo_bars
        test_end = test_start + test_bars
        if test_end > total_bars:
            break
        splits.append((range(start, train_end), range(test_start, test_end)))
        start += test_bars + embargo_bars
    return splits


def deflated_sharpe_ratio(returns: Sequence[float], n_trials: int = 1) -> float:
    """Compute simplified Deflated Sharpe Ratio (Bailey & Lopez de Prado) adjusting for multiple trials."""
    n = len(returns)
    if n < 3:
        return 0.0
    arr = np.asarray(returns, dtype=np.float64)
    std = float(np.std(arr, ddof=1))
    if std <= 1e-12:
        return 0.0
    sr = float(np.mean(arr)) / std
    if n_trials <= 1:
        return round(sr, 6)

    norm = NormalDist()
    emc = 0.5772156649
    p1 = max(1e-6, min(1.0 - 1e-6, 1.0 - 1.0 / n_trials))
    p2 = max(1e-6, min(1.0 - 1e-6, 1.0 - 1.0 / (n_trials * math.e)))
    expected_max_z = (1.0 - emc) * norm.inv_cdf(p1) + emc * norm.inv_cdf(p2)
    sr_threshold = expected_max_z / math.sqrt(max(1, n - 1))
    return round(sr - sr_threshold, 6)
