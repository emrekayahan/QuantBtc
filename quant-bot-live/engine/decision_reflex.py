"""Layer 2 Reflex Decision Scorer: evaluates every closed candle in <50ms and outputs ReflexDecisionSchema."""

from __future__ import annotations

from typing import Optional

from config.settings import Settings, settings
from database.db_manager import DatabaseManager
from engine.schemas import (
    DirectionLiteral,
    ReflexDecisionSchema,
    RiskStateLiteral,
    StateSnapshotSchema,
)


class DecisionReflexScorer:
    """Fast, calibrated deterministic decision scorer (<50ms execution)."""

    def __init__(
        self,
        db: Optional[DatabaseManager] = None,
        custom_settings: Optional[Settings] = None,
    ) -> None:
        self.db = db
        self.cfg = custom_settings or settings

    def evaluate(self, state: StateSnapshotSchema) -> ReflexDecisionSchema:
        """
        Evaluate a causal StateSnapshotSchema and output a typed ReflexDecisionSchema.
        100% deterministic math, zero external network calls.
        """
        entry_price = float(state.closed_close)
        atr = float(state.atr_14)

        # Invalidation price (Stop-Loss) at entry_price - 2.0 * ATR(14), capped at max 1.8% distance from entry
        raw_stop_dist = 2.0 * atr
        max_stop_dist = entry_price * 0.018
        min_stop_dist = entry_price * 0.003
        stop_dist = max(min_stop_dist, min(raw_stop_dist, max_stop_dist))

        invalidation_price = max(1e-8, entry_price - stop_dist)
        take_profit_price = entry_price + 2.5 * (entry_price - invalidation_price)
        expected_rr = (take_profit_price - entry_price) / max(entry_price - invalidation_price, 1e-9)

        # Risk state evaluation
        max_dd = float(self.cfg.MAX_DRAWDOWN_PCT)
        max_open = int(self.cfg.MAX_OPEN_POSITIONS)
        if (
            state.regime == "crisis"
            or state.current_drawdown_pct >= max_dd * 0.85
            or state.open_positions_count >= max_open
        ):
            risk_state: RiskStateLiteral = "reduce"
        elif (
            state.current_drawdown_pct >= max_dd * 0.60
            or state.open_positions_count == max_open - 1
            or state.spread_bps > 12.0
        ):
            risk_state = "near_limit"
        else:
            risk_state = "safe"

        direction: DirectionLiteral = "neutral"
        setup_quality: int = 0
        confidence: float = 0.25
        reasons: list[str] = []

        # Crisis or toxic flow blocks bullish setups
        if state.regime == "crisis":
            reasons.append("Macro crisis regime active; all long entries blocked.")
        elif state.toxic_flow != "none":
            reasons.append(f"Toxic orderflow detected ({state.toxic_flow}); standing down.")
        elif state.regime == "trending":
            # Sub-Strategy 1: Trend Momentum (regime == 'trending')
            # Spec: Price > EMA(200), EMA(9) > EMA(21), 48 <= RSI(14) <= 68,
            # Volume > 1.2 * SMA(Vol, 20), and orderbook_imbalance > -0.15
            cond_macro_up = state.closed_close > state.ema_200
            cond_ema_cross = state.ema_9 > state.ema_21
            cond_rsi_band = 48.0 <= state.rsi_14 <= 68.0
            cond_vol_surge = state.vol_sma_20 > 0 and state.closed_volume > 1.2 * state.vol_sma_20
            cond_ob_support = state.orderbook_imbalance > -0.15

            passed = sum([cond_macro_up, cond_ema_cross, cond_rsi_band, cond_vol_surge, cond_ob_support])

            if cond_macro_up and cond_ema_cross and cond_rsi_band and cond_vol_surge and cond_ob_support:
                direction = "long"
                # Bonus quality check: strong ADX and positive orderbook imbalance + EMA50 alignment
                if state.closed_close > state.ema_50 and state.orderbook_imbalance >= 0.05 and state.adx_14 >= 25.0:
                    setup_quality = 3
                    confidence = min(
                        0.95,
                        0.85
                        + min(0.05, max(0.0, state.orderbook_imbalance) * 0.10)
                        + min(0.05, max(0.0, (state.adx_14 - 25.0) / 200.0)),
                    )
                else:
                    setup_quality = 2
                    confidence = min(
                        0.92,
                        0.82 + min(0.06, max(0.0, state.orderbook_imbalance) * 0.10),
                    )
                reasons.append(
                    f"Trend Momentum Long: Close({state.closed_close:.4f})>EMA200({state.ema_200:.4f}), "
                    f"EMA9>EMA21, RSI={state.rsi_14:.1f}, Vol={state.closed_volume / max(state.vol_sma_20, 1e-9):.2f}x, "
                    f"OB_Imb={state.orderbook_imbalance:+.2f}"
                )
            elif passed >= 3 and cond_macro_up:
                direction = "long"
                setup_quality = 1
                confidence = 0.62
                reasons.append(f"Partial Trend setup ({passed}/5 conditions met); below execution threshold.")
            else:
                reasons.append(f"No Trend setup ({passed}/5 conditions met).")

        elif state.regime in ("mean_reverting", "high_vol"):
            # Sub-Strategy 2: Mean Reversion Dip (regime == 'mean_reverting')
            # Spec: Price > EMA(200) (macro uptrend intact), Price touches lower Bollinger Band (20, 2sigma),
            # RSI(14) < 34 turning up on closed candle
            cond_macro_up = state.closed_close > state.ema_200
            cond_bb_touch = state.closed_low <= state.bb_lower * 1.002 or state.closed_close <= state.bb_lower * 1.005
            cond_rsi_oversold_turn = state.rsi_14 < 34.0 and state.rsi_14 > state.prev_rsi_14
            cond_ob_ok = state.orderbook_imbalance > -0.35

            if cond_macro_up and cond_bb_touch and cond_rsi_oversold_turn and cond_ob_ok:
                direction = "long"
                if state.orderbook_imbalance >= 0.0 and state.closed_close > state.closed_open:
                    setup_quality = 3
                    confidence = min(0.95, 0.86 + min(0.06, max(0.0, state.orderbook_imbalance) * 0.12))
                else:
                    setup_quality = 2
                    confidence = 0.83
                reasons.append(
                    f"Mean Reversion Dip Long: Close({state.closed_close:.4f})>EMA200({state.ema_200:.4f}), "
                    f"Low({state.closed_low:.4f})<=BB_Lower({state.bb_lower:.4f}), "
                    f"RSI({state.prev_rsi_14:.1f}->{state.rsi_14:.1f}) turning up"
                )
            elif cond_macro_up and (cond_bb_touch or cond_rsi_oversold_turn):
                direction = "long"
                setup_quality = 1
                confidence = 0.58
                reasons.append("Developing Mean Reversion dip; awaiting full RSI turn + BB touch.")
            else:
                reasons.append("Neutral oscillating state inside Bollinger Bands.")

        decision = ReflexDecisionSchema(
            symbol=state.symbol,
            causal_timestamp_ms=state.causal_timestamp_ms,
            regime=state.regime,
            direction=direction,
            toxic_flow=state.toxic_flow,
            setup_quality=setup_quality,
            direction_confidence=round(float(confidence), 4),
            risk_state=risk_state,
            entry_price=round(entry_price, 8),
            invalidation_price=round(invalidation_price, 8),
            take_profit_price=round(take_profit_price, 8),
            expected_reward_risk_ratio=round(expected_rr, 4),
            rationale=" | ".join(reasons),
        )

        if self.db is not None:
            self.db.update_watchlist_telemetry(
                symbol=state.symbol,
                rsi_14=state.rsi_14,
                setup_quality=decision.setup_quality,
                direction_confidence=decision.direction_confidence,
                regime=state.regime,
                toxic_flow=state.toxic_flow,
                spread_bps=state.spread_bps,
                price=state.mid_price,
            )

        return decision
