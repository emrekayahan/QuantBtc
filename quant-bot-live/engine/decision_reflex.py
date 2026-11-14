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

        # Invalidation price (Stop-Loss) at 2.0 * ATR(14), capped at max 1.8% distance from entry
        raw_stop_dist = 2.0 * atr
        max_stop_dist = entry_price * 0.018
        min_stop_dist = entry_price * 0.003
        stop_dist = max(min_stop_dist, min(raw_stop_dist, max_stop_dist))

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
            or state.spread_bps > 20.0
        ):
            risk_state = "near_limit"
        else:
            risk_state = "safe"

        direction: DirectionLiteral = "neutral"
        strategy_id: str = "default"
        setup_quality: int = 0
        confidence: float = 0.25
        reasons: list[str] = []

        # R14 Check: BLOCK_LONGS_BELOW_BTC_EMA200
        block_longs_below_btc = getattr(self.cfg, "BLOCK_LONGS_BELOW_BTC_EMA200", True)
        btc_below_ema200 = False
        if self.db is not None:
            btc_macro = str(self.db.get_state("btc_below_ema200", "false")).lower() in ("true", "1")
            if btc_macro:
                btc_below_ema200 = True

        bb_touch_tol = float(getattr(self.cfg, "BB_TOUCH_TOL", 0.002))
        mom_min_atr_frac = float(getattr(self.cfg, "MOMENTUM_MIN_ATR_FRAC", 0.3))
        adx_min = float(getattr(self.cfg, "ADX_MIN", 20.0))

        # Crisis or toxic flow blocks setups
        if state.regime == "crisis":
            reasons.append("Macro crisis regime active; all entries blocked.")
        elif state.toxic_flow != "none":
            reasons.append(f"Toxic orderflow detected ({state.toxic_flow}); standing down.")
        elif state.regime == "trending":
            # Sub-Strategy 1: Trend Momentum (Long & Short)
            enable_short = bool(getattr(self.cfg, "ENABLE_SHORT", True))
            cond_macro_up = state.closed_close > state.ema_200
            cond_ema_cross = state.ema_9 > state.ema_21
            cond_rsi_band = 48.0 <= state.rsi_14 <= 68.0
            cond_vol_surge = state.vol_sma_20 > 0 and state.closed_volume > 1.1 * state.vol_sma_20
            cond_ob_support = state.orderbook_imbalance > -0.15

            passed_long = sum([cond_macro_up, cond_ema_cross, cond_rsi_band, cond_vol_surge, cond_ob_support])

            cond_macro_down = state.closed_close < state.ema_200
            cond_ema_cross_down = state.ema_9 < state.ema_21
            cond_rsi_band_down = 32.0 <= state.rsi_14 <= 52.0
            cond_ob_resist = state.orderbook_imbalance < 0.15
            passed_short = sum([cond_macro_down, cond_ema_cross_down, cond_rsi_band_down, cond_vol_surge, cond_ob_resist])

            if passed_long == 5 or (passed_long >= 4 and cond_macro_up and cond_ema_cross):
                if block_longs_below_btc and btc_below_ema200:
                    reasons.append("Long entry blocked: BTC is below its EMA200.")
                else:
                    direction = "long"
                    strategy_id = "trend_momentum"
                    if passed_long == 5 and state.closed_close > state.ema_50 and state.orderbook_imbalance >= 0.05 and state.adx_14 >= 25.0:
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
                            (0.82 if passed_long == 5 else 0.74) + min(0.06, max(0.0, state.orderbook_imbalance) * 0.10),
                        )
                    reasons.append(
                        f"Trend Momentum Long ({passed_long}/5): Close({state.closed_close:.4f})>EMA200({state.ema_200:.4f}), "
                        f"EMA9>EMA21, RSI={state.rsi_14:.1f}, Vol={state.closed_volume / max(state.vol_sma_20, 1e-9):.2f}x, "
                        f"OB_Imb={state.orderbook_imbalance:+.2f}"
                    )
            elif enable_short and (passed_short == 5 or (passed_short >= 4 and cond_macro_down and cond_ema_cross_down)):
                direction = "short"
                strategy_id = "bear_trend_momentum"
                if passed_short == 5 and state.closed_close < state.ema_50 and state.orderbook_imbalance <= -0.05 and state.adx_14 >= 25.0:
                    setup_quality = 3
                    confidence = min(
                        0.95,
                        0.85
                        + min(0.05, max(0.0, -state.orderbook_imbalance) * 0.10)
                        + min(0.05, max(0.0, (state.adx_14 - 25.0) / 200.0)),
                    )
                else:
                    setup_quality = 2
                    confidence = min(
                        0.92,
                        (0.82 if passed_short == 5 else 0.74) + min(0.06, max(0.0, -state.orderbook_imbalance) * 0.10),
                    )
                reasons.append(
                    f"Bear Trend Momentum Short ({passed_short}/5): Close({state.closed_close:.4f})<EMA200({state.ema_200:.4f}), "
                    f"EMA9<EMA21, RSI={state.rsi_14:.1f}, Vol={state.closed_volume / max(state.vol_sma_20, 1e-9):.2f}x, "
                    f"OB_Imb={state.orderbook_imbalance:+.2f}"
                )
            elif passed_long >= 3 and cond_macro_up and not (block_longs_below_btc and btc_below_ema200):
                direction = "long"
                strategy_id = "trend_momentum"
                setup_quality = 1
                confidence = 0.62
                reasons.append(f"Partial Trend setup ({passed_long}/5 conditions met); below execution threshold.")
            elif enable_short and passed_short >= 3 and cond_macro_down:
                direction = "short"
                strategy_id = "bear_trend_momentum"
                setup_quality = 1
                confidence = 0.62
                reasons.append(f"Partial Bear Trend setup ({passed_short}/5 conditions met); below execution threshold.")
            else:
                reasons.append(f"No Trend setup (long={passed_long}/5, short={passed_short}/5 conditions met).")

        elif state.regime in ("mean_reverting", "high_vol"):
            # Sub-Strategy 2: Regime-Aware Mean Reversion (Long & Short) OR Intraday Momentum (Bounce/Breakdown)
            enable_short = bool(getattr(self.cfg, "ENABLE_SHORT", True))
            candle_range = max(state.closed_high - state.closed_low, 1e-9)
            lower_wick = min(state.closed_open, state.closed_close) - state.closed_low
            upper_wick = state.closed_high - max(state.closed_open, state.closed_close)

            cond_buying_pressure = (
                state.closed_close >= state.closed_open
                or state.rsi_14 > state.prev_rsi_14
                or (lower_wick / candle_range) >= 0.30
            )
            cond_bb_support = state.closed_low <= state.bb_lower * (1.0 + bb_touch_tol)
            cond_rsi_oversold = state.rsi_14 < 34.0 or (
                state.rsi_14 < 36.0 and state.rsi_14 > state.prev_rsi_14 and cond_bb_support
            )
            cond_intraday_momentum = (
                (state.closed_close - state.ema_21) >= (mom_min_atr_frac * state.atr_14)
                and state.adx_14 >= adx_min
                and state.ema_9 > state.ema_21
                and 50.0 <= state.rsi_14 <= 65.0
                and state.closed_close > state.closed_open
                and state.rsi_14 >= state.prev_rsi_14 + 0.3
                and (state.vol_sma_20 <= 0 or state.closed_volume >= state.vol_sma_20 * 1.0)
            )
            cond_ob_ok = state.orderbook_imbalance > -0.25

            # Short symmetric conditions
            cond_selling_pressure = (
                state.closed_close <= state.closed_open
                or state.rsi_14 < state.prev_rsi_14
                or (upper_wick / candle_range) >= 0.30
            )
            cond_bb_resist = state.closed_high >= state.bb_upper * (1.0 - bb_touch_tol)
            cond_rsi_overbought = state.rsi_14 > 66.0 or (
                state.rsi_14 > 64.0 and state.rsi_14 < state.prev_rsi_14 and cond_bb_resist
            )
            cond_intraday_breakdown = (
                (state.ema_21 - state.closed_close) >= (mom_min_atr_frac * state.atr_14)
                and state.adx_14 >= adx_min
                and state.ema_9 < state.ema_21
                and 35.0 <= state.rsi_14 <= 52.0
                and state.closed_close < state.closed_open
                and state.rsi_14 <= state.prev_rsi_14 - 0.3
                and (state.vol_sma_20 <= 0 or state.closed_volume >= state.vol_sma_20 * 1.0)
            )
            cond_ob_sell_ok = state.orderbook_imbalance < 0.25

            if cond_rsi_oversold and cond_bb_support and cond_buying_pressure and cond_ob_ok:
                if block_longs_below_btc and btc_below_ema200:
                    reasons.append("Long entry blocked: BTC is below its EMA200.")
                else:
                    direction = "long"
                    strategy_id = "mean_reversion_dip"
                    rsi_bonus = min(0.08, max(0.0, (36.0 - state.rsi_14) * 0.008))
                    ob_bonus = min(0.06, max(0.0, state.orderbook_imbalance) * 0.12)
                    if state.closed_close > state.closed_open and (
                        state.orderbook_imbalance >= 0.0 or state.rsi_14 < 28.0
                    ):
                        setup_quality = 3
                        confidence = min(0.94, 0.78 + rsi_bonus + ob_bonus)
                    else:
                        setup_quality = 2
                        confidence = min(0.88, max(0.73, 0.73 + rsi_bonus + ob_bonus))
                    reasons.append(
                        f"Mean Reversion Dip Long: Low({state.closed_low:.4f})~BB_Lower({state.bb_lower:.4f}), "
                        f"RSI={state.rsi_14:.1f} (prev {state.prev_rsi_14:.1f}), "
                        f"BuyingPressure=True, OB_Imb={state.orderbook_imbalance:+.2f}"
                    )
            elif enable_short and cond_rsi_overbought and cond_bb_resist and cond_selling_pressure and cond_ob_sell_ok:
                direction = "short"
                strategy_id = "mean_reversion_rejection_short"
                rsi_bonus = min(0.08, max(0.0, (state.rsi_14 - 64.0) * 0.008))
                ob_bonus = min(0.06, max(0.0, -state.orderbook_imbalance) * 0.12)
                if state.closed_close < state.closed_open and (
                    state.orderbook_imbalance <= 0.0 or state.rsi_14 > 72.0
                ):
                    setup_quality = 3
                    confidence = min(0.94, 0.78 + rsi_bonus + ob_bonus)
                else:
                    setup_quality = 2
                    confidence = min(0.88, max(0.73, 0.73 + rsi_bonus + ob_bonus))
                reasons.append(
                    f"Mean Reversion Rejection Short: High({state.closed_high:.4f})~BB_Upper({state.bb_upper:.4f}), "
                    f"RSI={state.rsi_14:.1f} (prev {state.prev_rsi_14:.1f}), "
                    f"SellingPressure=True, OB_Imb={state.orderbook_imbalance:+.2f}"
                )
            elif cond_intraday_momentum and cond_ob_ok:
                if block_longs_below_btc and btc_below_ema200:
                    reasons.append("Long entry blocked: BTC is below its EMA200.")
                else:
                    direction = "long"
                    strategy_id = "intraday_bounce"
                    ob_bonus = min(0.06, max(0.0, state.orderbook_imbalance) * 0.12)
                    if state.closed_close > state.ema_50 and state.orderbook_imbalance >= 0.05:
                        setup_quality = 3
                        confidence = min(0.90, 0.78 + ob_bonus)
                    else:
                        setup_quality = 2
                        confidence = min(0.85, 0.73 + ob_bonus)
                    reasons.append(
                        f"Intraday Momentum Bounce Long: Close({state.closed_close:.4f})>EMA21({state.ema_21:.4f}), "
                        f"RSI={state.rsi_14:.1f} rising, OB_Imb={state.orderbook_imbalance:+.2f}"
                    )
            elif enable_short and cond_intraday_breakdown and cond_ob_sell_ok:
                direction = "short"
                strategy_id = "intraday_breakdown_short"
                ob_bonus = min(0.06, max(0.0, -state.orderbook_imbalance) * 0.12)
                if state.closed_close < state.ema_50 and state.orderbook_imbalance <= -0.05:
                    setup_quality = 3
                    confidence = min(0.90, 0.78 + ob_bonus)
                else:
                    setup_quality = 2
                    confidence = min(0.85, 0.73 + ob_bonus)
                reasons.append(
                    f"Intraday Breakdown Short: Close({state.closed_close:.4f})<EMA21({state.ema_21:.4f}), "
                    f"RSI={state.rsi_14:.1f} falling, OB_Imb={state.orderbook_imbalance:+.2f}"
                )
            elif cond_bb_support and cond_rsi_oversold and cond_ob_ok:
                if not (block_longs_below_btc and btc_below_ema200):
                    direction = "long"
                    strategy_id = "mean_reversion_dip"
                    setup_quality = 1
                    confidence = 0.60
                    reasons.append(
                        f"Developing Mean Reversion dip (RSI={state.rsi_14:.1f}, Low={state.closed_low:.4f} vs BB_Low={state.bb_lower:.4f}); awaiting confirmation."
                    )
            elif enable_short and cond_bb_resist and cond_rsi_overbought and cond_ob_sell_ok:
                direction = "short"
                strategy_id = "mean_reversion_rejection_short"
                setup_quality = 1
                confidence = 0.60
                reasons.append(
                    f"Developing Mean Reversion rejection (RSI={state.rsi_14:.1f}, High={state.closed_high:.4f} vs BB_High={state.bb_upper:.4f}); awaiting confirmation."
                )
            else:
                reasons.append("Neutral oscillating state inside Bollinger Bands.")

        if direction == "short":
            invalidation_price = entry_price + stop_dist
            take_profit_price = max(1e-8, entry_price - 2.5 * stop_dist)
            expected_rr = (entry_price - take_profit_price) / max(invalidation_price - entry_price, 1e-9)
        else:
            invalidation_price = max(1e-8, entry_price - stop_dist)
            take_profit_price = entry_price + 2.5 * (entry_price - invalidation_price)
            expected_rr = (take_profit_price - entry_price) / max(entry_price - invalidation_price, 1e-9)

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
            strategy_id=strategy_id,
            side=direction,
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
