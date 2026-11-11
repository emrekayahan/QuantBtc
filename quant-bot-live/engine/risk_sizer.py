"""Risk-based position sizing and margin/exposure engineering.

Pure functional sizing calculations based on FIX_PLAN_ROUND2 Section 1.2 and R3.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional


class RiskSizer:
    """Computes exact risk-based notional, margin, and leverage."""

    @staticmethod
    def calculate(
        equity: float,
        entry_price: float,
        invalidation_price: float,
        min_notional: float,
        trading_mode: str,
        free_usdt: float,
        futures_free_usdt: float,
        open_positions: List[Dict[str, Any]],
        cfg: Any,
        f_quarter: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        Pure sizing calculation with strict guardrails:
        1. Small account threshold detection (default < $100)
        2. Unit risk = |entry - sl|/entry + 2*fee + slippage
        3. Risk budget = equity * effective_risk_pct
        4. Gross exposure includes incoming notional <= equity * MAX_GROSS_EXPOSURE_X
        5. Total open risk includes incoming trade risk <= equity * MAX_TOTAL_OPEN_RISK_PCT
        6. Futures isolated leverage respects LIQ_BUFFER_X (>= 2x SL distance) and margin cap
        """
        if equity <= 0.0 or entry_price <= 0.0 or invalidation_price <= 0.0:
            return {
                "approved": False,
                "position_usd": 0.0,
                "margin_usd": 0.0,
                "leverage": 1,
                "trading_mode": trading_mode,
                "reject_code": "INVALID_INPUTS",
                "reason": "Invalid non-positive inputs for equity or prices.",
            }

        is_small = equity < float(getattr(cfg, "SMALL_ACCOUNT_EQUITY_USD", 100.0))
        base_risk_pct = float(
            getattr(cfg, "SMALL_ACCOUNT_MAX_RISK_PCT", 1.5)
            if is_small
            else getattr(cfg, "MAX_RISK_PER_TRADE_PCT", 1.0)
        )
        risk_budget_usd = equity * (base_risk_pct / 100.0)

        sl_dist_pct = abs(entry_price - invalidation_price) / entry_price
        fee_pct = float(
            getattr(cfg, "SPOT_FEE_PCT", 0.10)
            if trading_mode.upper() == "SPOT"
            else getattr(cfg, "FUTURES_FEE_PCT", 0.05)
        ) / 100.0
        slippage_pct = float(getattr(cfg, "SLIPPAGE_PCT", 0.05)) / 100.0
        unit_risk = sl_dist_pct + (2.0 * fee_pct) + slippage_pct
        if unit_risk <= 0.0:
            unit_risk = 0.01

        max_pos_usd = float(getattr(cfg, "MAX_POSITION_USD", 25.0))
        target_notional = min(risk_budget_usd / unit_risk, max_pos_usd)

        # Incorporate Quarter-Kelly fraction if provided
        if f_quarter is not None and f_quarter > 0.0:
            target_notional = min(target_notional, free_usdt * f_quarter)

        # Min Notional & Exception Check
        required_min_notional = float(min_notional) * 1.10
        if trading_mode.upper() == "SPOT":
            spot_min = float(getattr(cfg, "SPOT_MIN_POSITION_USD", 11.50))
            required_min_notional = max(required_min_notional, spot_min)

        if target_notional < required_min_notional:
            # Check small account exception budget
            exception_risk_pct = float(getattr(cfg, "MIN_NOTIONAL_RISK_EXCEPTION_PCT", 3.0))
            exception_budget_usd = equity * (exception_risk_pct / 100.0)
            exception_notional = exception_budget_usd / unit_risk

            if is_small and required_min_notional <= exception_notional:
                # Allowed under small account exception
                target_notional = min(required_min_notional, max_pos_usd)
            elif (not is_small) and (required_min_notional * unit_risk) <= risk_budget_usd:
                target_notional = min(required_min_notional, max_pos_usd)
            else:
                return {
                    "approved": False,
                    "position_usd": 0.0,
                    "margin_usd": 0.0,
                    "leverage": 1,
                    "trading_mode": trading_mode,
                    "reject_code": "MIN_NOTIONAL_EXCEEDS_RISK_BUDGET",
                    "reason": (
                        f"Required min notional (${required_min_notional:.2f}) exceeds risk budget "
                        f"(${risk_budget_usd:.2f}) and max exception cap (${exception_budget_usd:.2f})."
                    ),
                }

        target_notional = round(target_notional, 2)
        trade_risk_usd = round(target_notional * unit_risk, 4)
        trade_risk_pct = round((trade_risk_usd / equity) * 100.0, 2)

        # 1. Gross Exposure Guardrail (MUST include incoming trade notional)
        current_notional = sum(
            float(p.get("notional_usd", p.get("cost_usd", 0.0))) for p in open_positions
        )
        total_gross_notional = current_notional + target_notional
        max_gross = equity * float(getattr(cfg, "MAX_GROSS_EXPOSURE_X", 1.0))
        if total_gross_notional > max_gross and max_gross > 0.0:
            return {
                "approved": False,
                "position_usd": target_notional,
                "margin_usd": 0.0,
                "leverage": 1,
                "trading_mode": trading_mode,
                "reject_code": "GROSS_EXPOSURE_CAP_EXCEEDED",
                "reason": (
                    f"Total gross notional (${total_gross_notional:.2f} = open ${current_notional:.2f} + new ${target_notional:.2f}) "
                    f"exceeds cap (${max_gross:.2f} at {cfg.MAX_GROSS_EXPOSURE_X}x)."
                ),
            }

        # 2. Total Open Risk Guardrail
        current_open_risk = 0.0
        for p in open_positions:
            p_entry = float(p.get("entry_price", 1.0))
            p_sl = float(p.get("invalidation_price", p_entry))
            p_dist = abs(p_entry - p_sl) / (p_entry or 1.0)
            p_notional = float(p.get("notional_usd", p.get("cost_usd", 0.0)))
            current_open_risk += p_notional * (p_dist + (2.0 * fee_pct) + slippage_pct)

        max_total_risk_pct = float(getattr(cfg, "MAX_TOTAL_OPEN_RISK_PCT", 3.0))
        if is_small:
            max_total_risk_pct = max(max_total_risk_pct, 5.0)
        max_total_risk_usd = equity * (max_total_risk_pct / 100.0)

        if (current_open_risk + trade_risk_usd) > max_total_risk_usd:
            return {
                "approved": False,
                "position_usd": target_notional,
                "margin_usd": 0.0,
                "leverage": 1,
                "trading_mode": trading_mode,
                "reject_code": "TOTAL_OPEN_RISK_CAP_EXCEEDED",
                "reason": (
                    f"Total open risk with new trade (${current_open_risk + trade_risk_usd:.2f}) "
                    f"exceeds limit (${max_total_risk_usd:.2f} at {max_total_risk_pct}%)."
                ),
            }

        # 3. Market Specific Rules
        if trading_mode.upper() == "SPOT":
            if free_usdt < (target_notional * 1.001):
                # Check if we can scale down to available free_usdt
                scalable_notional = free_usdt * 0.98
                if scalable_notional >= required_min_notional:
                    target_notional = round(scalable_notional, 2)
                    trade_risk_usd = round(target_notional * unit_risk, 4)
                else:
                    return {
                        "approved": False,
                        "position_usd": target_notional,
                        "margin_usd": 0.0,
                        "leverage": 1,
                        "trading_mode": "SPOT",
                        "reject_code": "INSUFFICIENT_SPOT_CASH",
                        "reason": f"Available free USDT (${free_usdt:.2f}) below required notional (${target_notional:.2f}).",
                    }
            return {
                "approved": True,
                "position_usd": target_notional,
                "margin_usd": target_notional,
                "leverage": 1,
                "trading_mode": "SPOT",
                "risk_usd": trade_risk_usd,
                "risk_pct": trade_risk_pct,
                "liq_distance_pct": 100.0,
                "reason": f"Spot risk-based sizing approved: ${target_notional:.2f} (Risk: ${trade_risk_usd:.2f} / {trade_risk_pct}%).",
            }

        # FUTURES SIZING
        liq_buffer_x = float(getattr(cfg, "LIQ_BUFFER_X", 2.0))
        maint_margin_rate = 0.005  # 0.5% standard Binance maintenance margin
        req_liq_dist = liq_buffer_x * sl_dist_pct
        if (req_liq_dist + maint_margin_rate) >= 1.0:
            return {
                "approved": False,
                "position_usd": 0.0,
                "margin_usd": 0.0,
                "leverage": 1,
                "trading_mode": "FUTURES",
                "reject_code": "LIQ_BUFFER_UNSATISFIABLE",
                "reason": f"Required liquidation buffer ({liq_buffer_x}x SL = {req_liq_dist*100:.1f}%) exceeds 100%.",
            }

        max_leverage_by_liq = int(1.0 / (req_liq_dist + maint_margin_rate))
        max_configured_lev = int(getattr(cfg, "MAX_LEVERAGE", 3))
        allowed_lev_ceiling = max(1, min(max_leverage_by_liq, max_configured_lev))

        # Margin cap per trade (default 25% equity)
        max_margin_pct = float(getattr(cfg, "MAX_MARGIN_PCT_PER_TRADE", 25.0))
        max_margin_allowed = equity * (max_margin_pct / 100.0)

        # Minimum leverage needed to keep margin <= max_margin_allowed
        min_lev_needed = math.ceil(target_notional / max(max_margin_allowed, 0.01))
        if min_lev_needed > allowed_lev_ceiling:
            return {
                "approved": False,
                "position_usd": target_notional,
                "margin_usd": 0.0,
                "leverage": allowed_lev_ceiling,
                "trading_mode": "FUTURES",
                "reject_code": "MARGIN_LIQ_CONFLICT",
                "reason": (
                    f"Trade requires leverage >= {min_lev_needed}x for margin cap (${max_margin_allowed:.2f}), "
                    f"but liquidation buffer restricts leverage to <= {allowed_lev_ceiling}x."
                ),
            }

        chosen_leverage = max(1, min(allowed_lev_ceiling, max(int(getattr(cfg, "FUTURES_DEFAULT_LEVERAGE", 3)), min_lev_needed)))
        margin_usd = round(target_notional / chosen_leverage, 2)

        if margin_usd > (futures_free_usdt * 0.98):
            return {
                "approved": False,
                "position_usd": target_notional,
                "margin_usd": margin_usd,
                "leverage": chosen_leverage,
                "trading_mode": "FUTURES",
                "reject_code": "INSUFFICIENT_FUTURES_CASH",
                "reason": f"Required futures margin (${margin_usd:.2f}) exceeds available futures cash (${futures_free_usdt:.2f}).",
            }

        liq_dist_pct = round(((1.0 / chosen_leverage) - maint_margin_rate) * 100.0, 2)

        return {
            "approved": True,
            "position_usd": target_notional,
            "margin_usd": margin_usd,
            "leverage": chosen_leverage,
            "trading_mode": "FUTURES",
            "risk_usd": trade_risk_usd,
            "risk_pct": trade_risk_pct,
            "liq_distance_pct": liq_dist_pct,
            "reason": (
                f"Futures risk-based sizing approved: ${target_notional:.2f} notional @ {chosen_leverage}x "
                f"(Margin: ${margin_usd:.2f}, Risk: ${trade_risk_usd:.2f} / {trade_risk_pct}%, LiqDist: {liq_dist_pct}%)."
            ),
        }
