"""K10: VolatilityCircuitBreaker — blocks new entries on extreme BTC 5m move or ATR% spike."""
from __future__ import annotations

from typing import Any, Dict


class VolatilityCircuitBreaker:
    """Blocks new entries when BTC 5m move > 1.5% or ATR% z-score > 3.0."""

    def __init__(self, max_btc_5m_pct: float = 1.5, max_atr_zscore: float = 3.0) -> None:
        self.max_btc_5m_pct = float(max_btc_5m_pct)
        self.max_atr_zscore = float(max_atr_zscore)

    def check_entry_allowed(self, btc_5m_move_pct: float, atr_pct_zscore: float) -> Dict[str, Any]:
        if abs(float(btc_5m_move_pct)) > self.max_btc_5m_pct:
            return {
                "allowed": False,
                "reason": f"btc_5m move {btc_5m_move_pct:.2f}% > {self.max_btc_5m_pct:.2f}%",
                "tighten_sl": True,
            }
        if float(atr_pct_zscore) > self.max_atr_zscore:
            return {
                "allowed": False,
                "reason": f"atr_zscore {atr_pct_zscore:.2f} > {self.max_atr_zscore:.2f}",
                "tighten_sl": True,
            }
        return {"allowed": True, "reason": "normal_volatility", "tighten_sl": False}
