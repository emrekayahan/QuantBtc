"""Engine component for resolving every emitted Q>=1 signal against subsequent 15m OHLCV bars."""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from config.settings import Settings, settings
from database.db_manager import DatabaseManager
from engine.exchange import BinanceExchangeWrapper

logger = logging.getLogger(__name__)


class OutcomeResolver:
    """
    R10 Outcome Resolver:
    Resolves unresolved signals from reflex_decisions into signal_outcomes.
    Rule: If SL and TP touch occur within the SAME candle, SL is considered touched FIRST.
    Horizon: SIGNAL_HORIZON_BARS (default 32).
    Net R: gross R - round trip fees - SLIPPAGE_PCT.
    """

    def __init__(
        self,
        db: DatabaseManager,
        exchange: BinanceExchangeWrapper,
        custom_settings: Optional[Settings] = None,
    ) -> None:
        self.db = db
        self.exchange = exchange
        self.cfg = custom_settings or settings

    @staticmethod
    def resolve_signal_with_candles(
        entry: float,
        sl: float,
        tp: float,
        side: str,
        candles: List[Dict[str, float]],
        fee_pct: float = 0.10,
        slippage_pct: float = 0.05,
        horizon_bars: int = 32,
    ) -> Dict[str, Any]:
        """
        Pure deterministic resolution of a signal against OHLCV candles.
        candles: list of dicts with keys 'open', 'high', 'low', 'close', 'timestamp'.
        Returns dict with result (TP/SL/TIMEOUT), r_gross, r_net, bars_to_exit, mae_r, mfe_r.
        """
        is_long = side.lower() == "long"
        risk_dist = abs(entry - sl)
        if risk_dist <= 1e-9:
            risk_dist = entry * 0.01

        reward_dist = abs(tp - entry)

        mae_dist = 0.0
        mfe_dist = 0.0
        bars_count = 0

        for i, c in enumerate(candles[:horizon_bars]):
            bars_count = i + 1
            high = float(c["high"])
            low = float(c["low"])

            if is_long:
                curr_mae = max(0.0, entry - low)
                curr_mfe = max(0.0, high - entry)
                mae_dist = max(mae_dist, curr_mae)
                mfe_dist = max(mfe_dist, curr_mfe)

                sl_hit = low <= sl
                tp_hit = high >= tp

                # CRITICAL RULE: If both touch on the same candle, SL touches FIRST
                if sl_hit and tp_hit:
                    r_gross = -1.0
                    total_fee_slip = (2.0 * fee_pct + slippage_pct) / 100.0
                    r_net = r_gross - (total_fee_slip * entry / risk_dist)
                    return {
                        "result": "SL",
                        "r_gross": round(r_gross, 4),
                        "r_net": round(r_net, 4),
                        "bars_to_exit": bars_count,
                        "mae_r": round(mae_dist / risk_dist, 4),
                        "mfe_r": round(mfe_dist / risk_dist, 4),
                    }
                elif sl_hit:
                    r_gross = -1.0
                    total_fee_slip = (2.0 * fee_pct + slippage_pct) / 100.0
                    r_net = r_gross - (total_fee_slip * entry / risk_dist)
                    return {
                        "result": "SL",
                        "r_gross": round(r_gross, 4),
                        "r_net": round(r_net, 4),
                        "bars_to_exit": bars_count,
                        "mae_r": round(mae_dist / risk_dist, 4),
                        "mfe_r": round(mfe_dist / risk_dist, 4),
                    }
                elif tp_hit:
                    r_gross = reward_dist / risk_dist
                    total_fee_slip = (2.0 * fee_pct + slippage_pct) / 100.0
                    r_net = r_gross - (total_fee_slip * entry / risk_dist)
                    return {
                        "result": "TP",
                        "r_gross": round(r_gross, 4),
                        "r_net": round(r_net, 4),
                        "bars_to_exit": bars_count,
                        "mae_r": round(mae_dist / risk_dist, 4),
                        "mfe_r": round(mfe_dist / risk_dist, 4),
                    }
            else:
                # Short
                curr_mae = max(0.0, high - entry)
                curr_mfe = max(0.0, entry - low)
                mae_dist = max(mae_dist, curr_mae)
                mfe_dist = max(mfe_dist, curr_mfe)

                sl_hit = high >= sl
                tp_hit = low <= tp

                if sl_hit and tp_hit:
                    r_gross = -1.0
                    total_fee_slip = (2.0 * fee_pct + slippage_pct) / 100.0
                    r_net = r_gross - (total_fee_slip * entry / risk_dist)
                    return {
                        "result": "SL",
                        "r_gross": round(r_gross, 4),
                        "r_net": round(r_net, 4),
                        "bars_to_exit": bars_count,
                        "mae_r": round(mae_dist / risk_dist, 4),
                        "mfe_r": round(mfe_dist / risk_dist, 4),
                    }
                elif sl_hit:
                    r_gross = -1.0
                    total_fee_slip = (2.0 * fee_pct + slippage_pct) / 100.0
                    r_net = r_gross - (total_fee_slip * entry / risk_dist)
                    return {
                        "result": "SL",
                        "r_gross": round(r_gross, 4),
                        "r_net": round(r_net, 4),
                        "bars_to_exit": bars_count,
                        "mae_r": round(mae_dist / risk_dist, 4),
                        "mfe_r": round(mfe_dist / risk_dist, 4),
                    }
                elif tp_hit:
                    r_gross = reward_dist / risk_dist
                    total_fee_slip = (2.0 * fee_pct + slippage_pct) / 100.0
                    r_net = r_gross - (total_fee_slip * entry / risk_dist)
                    return {
                        "result": "TP",
                        "r_gross": round(r_gross, 4),
                        "r_net": round(r_net, 4),
                        "bars_to_exit": bars_count,
                        "mae_r": round(mae_dist / risk_dist, 4),
                        "mfe_r": round(mfe_dist / risk_dist, 4),
                    }

        # If horizon reached without TP or SL
        last_close = float(candles[min(len(candles) - 1, horizon_bars - 1)]["close"]) if candles else entry
        if is_long:
            exit_diff = last_close - entry
        else:
            exit_diff = entry - last_close

        r_gross = exit_diff / risk_dist
        total_fee_slip = (2.0 * fee_pct + slippage_pct) / 100.0
        r_net = r_gross - (total_fee_slip * entry / risk_dist)

        return {
            "result": "TIMEOUT",
            "r_gross": round(r_gross, 4),
            "r_net": round(r_net, 4),
            "bars_to_exit": bars_count,
            "mae_r": round(mae_dist / risk_dist, 4),
            "mfe_r": round(mfe_dist / risk_dist, 4),
        }

    async def resolve_pending_signals(self) -> int:
        """Fetch unresolved decisions from reflex_decisions, evaluate with OHLCV, and save to signal_outcomes."""
        unresolved_query = """
            SELECT r.* FROM reflex_decisions r
            LEFT JOIN signal_outcomes s ON r.id = s.decision_id
            WHERE s.decision_id IS NULL AND r.setup_quality >= 1
            ORDER BY r.causal_timestamp_ms ASC LIMIT 50
        """
        with self.db._lock:
            cursor = self.db._conn.execute(unresolved_query)
            cols = [d[0] for d in cursor.description]
            decisions = [dict(zip(cols, r)) for r in cursor.fetchall()]

        resolved_count = 0
        horizon = int(getattr(self.cfg, "SIGNAL_HORIZON_BARS", 32))
        fee_pct = float(getattr(self.cfg, "SPOT_FEE_PCT", 0.10))
        slippage_pct = float(getattr(self.cfg, "SLIPPAGE_PCT", 0.05))

        for d in decisions:
            sym = d["symbol"]
            ts = int(d["causal_timestamp_ms"])
            try:
                # Fetch candles since decision timestamp
                raw_ohlcv = await self.exchange.fetch_ohlcv(
                    symbol=sym,
                    timeframe=self.cfg.TIMEFRAME,
                    since=ts,
                    limit=horizon + 5,
                )
                if not raw_ohlcv or len(raw_ohlcv) < 2:
                    continue

                candles = [
                    {
                        "timestamp": c[0],
                        "open": c[1],
                        "high": c[2],
                        "low": c[3],
                        "close": c[4],
                        "volume": c[5],
                    }
                    for c in raw_ohlcv
                    if c[0] > ts
                ]

                if not candles:
                    continue

                res = self.resolve_signal_with_candles(
                    entry=float(d["entry_price"]),
                    sl=float(d["invalidation_price"]),
                    tp=float(d["take_profit_price"]),
                    side=str(d.get("direction", "long")),
                    candles=candles,
                    fee_pct=fee_pct,
                    slippage_pct=slippage_pct,
                    horizon_bars=horizon,
                )

                # Save outcome
                outcome_data = {
                    "decision_id": d["id"],
                    "symbol": sym,
                    "strategy_id": str(d.get("strategy_id") or d.get("regime", "default")),
                    "side": str(d.get("direction", "long")),
                    "entry": float(d["entry_price"]),
                    "sl": float(d["invalidation_price"]),
                    "tp": float(d["take_profit_price"]),
                    "horizon_bars": horizon,
                    "result": res["result"],
                    "r_gross": res["r_gross"],
                    "r_net": res["r_net"],
                    "bars_to_exit": res["bars_to_exit"],
                    "mae_r": res["mae_r"],
                    "mfe_r": res["mfe_r"],
                    "regime": d.get("regime", "mean_reverting"),
                    "features": {"rationale": d.get("rationale")},
                    "resolved_at_ms": int(time.time() * 1000),
                }
                self.db.save_signal_outcome(outcome_data)
                resolved_count += 1
                self.db.set_state("resolver_consecutive_errors", "0")
            except Exception as exc:
                curr_err = int(self.db.get_state("resolver_errors") or 0) + 1
                curr_consec = int(self.db.get_state("resolver_consecutive_errors") or 0) + 1
                self.db.set_state("resolver_errors", str(curr_err))
                self.db.set_state("resolver_consecutive_errors", str(curr_consec))
                logger.warning("Could not resolve signal %s for %s: %s", d.get("id"), sym, exc)
                if curr_consec >= 10:
                    logger.error("OutcomeResolver has encountered %d consecutive resolution errors!", curr_consec)

        return resolved_count
