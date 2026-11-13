"""K1 Bar-by-bar Futures Backtester with zero lookahead, maker/taker fills, funding, and liquidation."""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

import pandas as pd

from config.settings import Settings, settings
from engine.cost_model import CostModel


@dataclass
class StrategySignal:
    strategy_id: str
    side: str  # "long" | "short"
    entry: float
    stop: float
    tp: float
    entry_type: str = "MAKER"  # "MAKER" | "TAKER"
    leverage: int = 3
    max_hold_bars: int = 32
    trail_atr_mult: float = 0.0
    regime: str = "unknown"
    signal_score: float = 0.0
    features: Dict[str, Any] = field(default_factory=dict)


@dataclass
class BacktestResult:
    trades: List[Dict[str, Any]]
    missed_signals: List[Dict[str, Any]]
    equity_curve: List[float]
    max_drawdown_pct: float
    cost_breakdown: Dict[str, float]
    regime_stats: Dict[str, Dict[str, Any]]


class BacktestEngine:
    """Deterministic bar-by-bar simulator sharing the same score(state) interface as live."""

    def __init__(
        self,
        custom_settings: Optional[Settings] = None,
        entry_maker_timeout_bars: Optional[int] = None,
        funding_rate_8h: float = 0.0001,
        bnb_discount: bool = False,
        initial_equity: float = 100.0,
        risk_per_trade_pct: float = 1.0,
        mmr: float = 0.005,
    ) -> None:
        self.cfg = custom_settings or settings
        self.cost_model = CostModel(custom_settings=self.cfg)
        self.entry_maker_timeout_bars = int(
            entry_maker_timeout_bars
            if entry_maker_timeout_bars is not None
            else self.cfg.ENTRY_MAKER_TIMEOUT_BARS
        )
        self.funding_rate_8h = float(funding_rate_8h)
        self.bnb_discount = bool(bnb_discount)
        self.initial_equity = float(initial_equity)
        self.risk_per_trade_pct = float(risk_per_trade_pct)
        self.mmr = float(mmr)

    @staticmethod
    def compute_liquidation_price(entry: float, leverage: int, side: str, mmr: float = 0.005) -> float:
        lev = max(1, int(leverage))
        if str(side).lower() == "long":
            return entry * max(0.0, 1.0 - (1.0 / lev) + mmr)
        return entry * (1.0 + (1.0 / lev) - mmr)

    def run(
        self,
        df_15m: pd.DataFrame,
        score_fn: Callable[[Dict[str, Any]], Optional[StrategySignal]],
        symbol: str = "BTC/USDT",
        df_1h: Optional[pd.DataFrame] = None,
        funding_by_ts: Optional[Dict[int, float]] = None,
        seed: int = 42,
    ) -> BacktestResult:
        """Run bar-by-bar backtest on strictly closed candles (zero lookahead)."""
        _ = seed  # reserved for any stochastic fill extensions; current sim is 100% deterministic
        n = len(df_15m)
        trades: List[Dict[str, Any]] = []
        missed_signals: List[Dict[str, Any]] = []
        equity = self.initial_equity
        peak_equity = equity
        max_dd_pct = 0.0
        equity_curve = [equity]

        pending: Optional[Dict[str, Any]] = None
        position: Optional[Dict[str, Any]] = None

        df_15m = df_15m.reset_index(drop=True)
        if df_1h is not None:
            df_1h = df_1h.reset_index(drop=True)

        for i in range(n):
            row = df_15m.iloc[i]
            ts = int(row["timestamp"])
            o = float(row["open"])
            h = float(row["high"])
            l = float(row["low"])
            c = float(row["close"])

            # 1. Check pending MAKER / TAKER order fill on bar i
            if position is None and pending is not None:
                sig: StrategySignal = pending["signal"]
                filled = False
                fill_price = sig.entry
                is_maker = str(sig.entry_type).upper() == "MAKER"

                if not is_maker:
                    filled = True
                    fill_price = o if o > 0 else sig.entry
                else:
                    if l <= sig.entry <= h:
                        filled = True
                        fill_price = sig.entry

                if filled:
                    risk_dist = abs(fill_price - sig.stop)
                    if risk_dist <= 1e-9:
                        risk_dist = fill_price * 0.01
                    liq_px = self.compute_liquidation_price(fill_price, sig.leverage, sig.side, self.mmr)
                    position = {
                        "signal": sig,
                        "entry_bar": i,
                        "entry_ts": ts,
                        "entry_price": fill_price,
                        "stop_price": sig.stop,
                        "tp_price": sig.tp,
                        "risk_dist": risk_dist,
                        "liq_price": liq_px,
                        "is_maker": is_maker,
                        "funding_intervals": 0,
                        "funding_pct_acc": 0.0,
                        "mae_dist": 0.0,
                        "mfe_dist": 0.0,
                    }
                    pending = None
                else:
                    pending["bars_waited"] += 1
                    if pending["bars_waited"] >= self.entry_maker_timeout_bars:
                        missed_signals.append(
                            {
                                "symbol": symbol,
                                "strategy_id": sig.strategy_id,
                                "side": sig.side,
                                "signal_bar": pending["signal_bar"],
                                "entry": sig.entry,
                                "status": "MISSED",
                            }
                        )
                        pending = None

            # 2. Manage open position on bar i
            if position is not None:
                sig = position["signal"]
                is_long = str(sig.side).lower() == "long"
                entry_px = position["entry_price"]
                stop_px = position["stop_price"]
                tp_px = position["tp_price"]
                liq_px = position["liq_price"]
                risk_dist = position["risk_dist"]
                bars_held = i - position["entry_bar"] + 1

                # Accumulate 8h funding every 32 bars (15m * 32 = 8h)
                if bars_held > 0 and bars_held % 32 == 0:
                    f_rate = (
                        float(funding_by_ts.get(ts, self.funding_rate_8h))
                        if funding_by_ts is not None
                        else self.funding_rate_8h
                    )
                    position["funding_intervals"] += 1
                    position["funding_pct_acc"] += self.cost_model.estimate_funding_cost_pct(
                        f_rate, hold_hours=8.0, side=sig.side
                    )

                if is_long:
                    position["mae_dist"] = max(position["mae_dist"], max(0.0, entry_px - l))
                    position["mfe_dist"] = max(position["mfe_dist"], max(0.0, h - entry_px))
                    liq_hit = l <= liq_px
                    sl_hit = l <= stop_px
                    tp_hit = h >= tp_px
                else:
                    position["mae_dist"] = max(position["mae_dist"], max(0.0, h - entry_px))
                    position["mfe_dist"] = max(position["mfe_dist"], max(0.0, entry_px - l))
                    liq_hit = h >= liq_px
                    sl_hit = h >= stop_px
                    tp_hit = l <= tp_px

                exit_reason: Optional[str] = None
                exit_px = c
                exit_taker = True

                # Precedence: LIQUIDATION (if reached before SL) -> SL -> TP -> TIME
                if liq_hit and ((is_long and liq_px >= stop_px) or (not is_long and liq_px <= stop_px) or not sl_hit):
                    exit_reason = "LIQUIDATION"
                    exit_px = liq_px
                elif sl_hit:
                    # CRITICAL RULE: SL before TP on same candle
                    exit_reason = "SL"
                    exit_px = stop_px
                elif tp_hit:
                    exit_reason = "TP"
                    exit_px = tp_px
                    exit_taker = False
                elif bars_held >= int(sig.max_hold_bars):
                    exit_reason = "TIME"
                    exit_px = c

                if exit_reason is not None:
                    r_gross = (
                        ((exit_px - entry_px) / risk_dist)
                        if is_long
                        else ((entry_px - exit_px) / risk_dist)
                    )
                    cost_info = self.cost_model.compute_cost_r(
                        entry=entry_px,
                        stop=stop_px,
                        side=sig.side,
                        market_type="FUTURES",
                        entry_maker=position["is_maker"],
                        exit_taker=exit_taker,
                        funding_rate_8h=0.0,
                        expected_hold_hours=0.0,
                        bnb_discount=self.bnb_discount,
                    )
                    stop_pct = max(cost_info["stop_pct"], 1e-6)
                    fee_r = (cost_info["entry_fee_pct"] + cost_info["exit_fee_pct"]) / stop_pct
                    slip_r = cost_info["slippage_pct"] / stop_pct
                    fund_r = position["funding_pct_acc"] / stop_pct
                    total_cost_r = fee_r + slip_r + fund_r
                    r_net = r_gross - total_cost_r

                    dt_utc = datetime.fromtimestamp(position["entry_ts"] / 1000.0, tz=timezone.utc)
                    pnl_usd = equity * (self.risk_per_trade_pct / 100.0) * r_net
                    equity = max(0.0, equity + pnl_usd)
                    peak_equity = max(peak_equity, equity)
                    dd = ((peak_equity - equity) / peak_equity * 100.0) if peak_equity > 0 else 0.0
                    max_dd_pct = max(max_dd_pct, dd)
                    equity_curve.append(round(equity, 4))

                    trades.append(
                        {
                            "symbol": symbol,
                            "strategy_id": sig.strategy_id,
                            "side": sig.side,
                            "regime": sig.regime,
                            "entry_bar": position["entry_bar"],
                            "exit_bar": i,
                            "entry_ts": position["entry_ts"],
                            "exit_ts": ts,
                            "date_utc": dt_utc.strftime("%Y-%m-%d"),
                            "hour_utc": dt_utc.hour,
                            "cluster_key": f"{dt_utc.strftime('%Y-%m-%d')}_{symbol}",
                            "entry_price": entry_px,
                            "exit_price": exit_px,
                            "exit_reason": exit_reason,
                            "r_gross": round(r_gross, 6),
                            "r_net": round(r_net, 6),
                            "fee_cost_r": round(fee_r, 6),
                            "slippage_cost_r": round(slip_r, 6),
                            "funding_cost_r": round(fund_r, 6),
                            "total_cost_r": round(total_cost_r, 6),
                            "mae_r": round(position["mae_dist"] / risk_dist, 4),
                            "mfe_r": round(position["mfe_dist"] / risk_dist, 4),
                        }
                    )
                    position = None
                    continue

            # 3. Evaluate score_fn ONLY on strictly closed slice df_15m[:i+1]
            if position is None and pending is None and i < n - 1:
                state: Dict[str, Any] = {
                    "symbol": symbol,
                    "df_15m": df_15m.iloc[: i + 1].copy(),
                    "timestamp": ts,
                }
                if df_1h is not None:
                    state["df_1h"] = df_1h[df_1h["timestamp"] <= ts].copy()
                sig_out = score_fn(state)
                if sig_out is not None:
                    pending = {"signal": sig_out, "signal_bar": i, "bars_waited": 0}

        # Aggregate cost breakdown & regime stats
        n_tr = max(1, len(trades))
        cost_breakdown = {
            "avg_fee_r": round(sum(t["fee_cost_r"] for t in trades) / n_tr, 6) if trades else 0.0,
            "avg_slippage_r": round(sum(t["slippage_cost_r"] for t in trades) / n_tr, 6) if trades else 0.0,
            "avg_funding_r": round(sum(t["funding_cost_r"] for t in trades) / n_tr, 6) if trades else 0.0,
            "avg_total_cost_r": round(sum(t["total_cost_r"] for t in trades) / n_tr, 6) if trades else 0.0,
        }
        reg_groups: Dict[str, List[float]] = defaultdict(list)
        for t in trades:
            reg_groups[str(t.get("regime", "unknown"))].append(float(t["r_net"]))
        regime_stats = {
            reg: {"n": len(vals), "mean_net_r": round(sum(vals) / len(vals), 6)}
            for reg, vals in reg_groups.items()
        }

        return BacktestResult(
            trades=trades,
            missed_signals=missed_signals,
            equity_curve=equity_curve,
            max_drawdown_pct=round(max_dd_pct, 4),
            cost_breakdown=cost_breakdown,
            regime_stats=regime_stats,
        )
