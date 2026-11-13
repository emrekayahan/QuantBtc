"""K6: LeverageAdvisor & RiskAllocator — bot chooses minimal safe leverage from risk & liq buffer."""
from __future__ import annotations

import math
from typing import Any, Dict, Optional

from config.settings import Settings, settings


class LeverageAdvisor:
    """Pure risk-first sizing and minimal safe leverage advisor."""

    def __init__(self, custom_settings: Optional[Settings] = None) -> None:
        self.cfg = custom_settings or settings

    @staticmethod
    def drawdown_throttle(drawdown_pct: float) -> float:
        """Scale risk smoothly from 1.0 (at 0% DD) down to 0.25 (at >=12% DD)."""
        dd = max(0.0, float(drawdown_pct))
        if dd <= 2.0:
            return 1.0
        if dd >= 12.0:
            return 0.25
        return max(0.25, 1.0 - 0.75 * ((dd - 2.0) / 10.0))

    @staticmethod
    def vol_throttle(atr_pct_zscore: float) -> float:
        """Reduce risk when volatility z-score spikes above 1.5."""
        z = float(atr_pct_zscore)
        if z <= 1.5:
            return 1.0
        if z >= 3.0:
            return 0.4
        return max(0.4, 1.0 - 0.4 * (z - 1.5))

    def advise(
        self,
        equity: float,
        stop_dist_pct: float,
        mu_lcb: float,
        sigma_r: float = 1.3,
        strategy_state: str = "learn",
        drawdown_pct: float = 0.0,
        atr_pct_zscore: float = 0.0,
        round_trip_cost_pct: Optional[float] = None,
        slippage_pct: Optional[float] = None,
        mmr: float = 0.005,
    ) -> Dict[str, Any]:
        """Compute risk_pct, notional_usd, minimal safe leverage L, and margin_usd."""
        eq = max(0.0, float(equity))
        stop_pct = max(0.1, float(stop_dist_pct))
        stop_frac = stop_pct / 100.0
        rt_cost_frac = (
            float(round_trip_cost_pct)
            if round_trip_cost_pct is not None
            else (float(self.cfg.FUTURES_FEE_MAKER_PCT) + float(self.cfg.FUTURES_FEE_TAKER_PCT))
        ) / 100.0
        slip_frac = float(slippage_pct if slippage_pct is not None else self.cfg.SLIPPAGE_PCT) / 100.0

        state = str(strategy_state).lower()
        learn_risk = float(self.cfg.LEARN_RISK_PCT)
        min_risk = float(self.cfg.RISK_MIN_PCT)
        hard_max_risk = float(self.cfg.MAX_RISK_PER_TRADE_PCT_HARD)

        if state == "learn" or float(mu_lcb) <= 0.0:
            base_risk_pct = learn_risk
            l_cap_state = int(self.cfg.LEVERAGE_CAP_LEARN)
        else:
            sig = max(0.2, float(sigma_r))
            kelly_r = max(0.0, float(mu_lcb)) / (sig ** 2)
            raw_risk = float(self.cfg.KELLY_FRACTION) * kelly_r * 100.0
            state_cap = (
                float(self.cfg.RISK_MAX_PCT_EXPLOIT_HIGH)
                if state == "exploit_high"
                else float(self.cfg.RISK_MAX_PCT_EXPLOIT_LOW)
            )
            base_risk_pct = max(min_risk, min(raw_risk, min(state_cap, hard_max_risk)))
            l_cap_state = (
                int(self.cfg.LEVERAGE_CAP_EXPLOIT_HIGH)
                if state == "exploit_high"
                else int(self.cfg.LEVERAGE_CAP_EXPLOIT_LOW)
            )

        throttled_risk_pct = base_risk_pct * self.drawdown_throttle(drawdown_pct) * self.vol_throttle(atr_pct_zscore)
        throttled_risk_pct = min(throttled_risk_pct, hard_max_risk)

        risk_usd = eq * (throttled_risk_pct / 100.0)
        denom = max(1e-6, stop_frac + rt_cost_frac + slip_frac)
        notional_usd = risk_usd / denom
        max_notional = float(self.cfg.MAX_POSITION_NOTIONAL_X) * eq
        notional_usd = min(notional_usd, max_notional)

        max_margin_usd = max(1e-6, eq * (float(self.cfg.MAX_MARGIN_PCT_PER_TRADE) / 100.0))
        l_min = max(1, math.ceil(notional_usd / max_margin_usd)) if eq > 0 else 1

        liq_buf = max(2.0, float(self.cfg.LIQ_BUFFER_X))
        req_liq_dist = liq_buf * stop_frac + mmr
        l_liq = max(1, math.floor(1.0 / max(1e-6, req_liq_dist)))

        l_cap = min(l_cap_state, int(self.cfg.MAX_LEVERAGE))
        leverage = max(1, min(l_min, l_liq, l_cap))
        margin_usd = notional_usd / leverage

        return {
            "risk_pct": round(throttled_risk_pct, 6),
            "risk_usd": round(risk_usd, 6),
            "notional_usd": round(notional_usd, 6),
            "leverage": int(leverage),
            "margin_usd": round(margin_usd, 6),
            "l_min": int(l_min),
            "l_liq": int(l_liq),
            "l_cap": int(l_cap),
        }
