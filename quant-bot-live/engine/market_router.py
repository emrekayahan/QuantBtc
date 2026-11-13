"""R15 Market Router: routes signals to Spot vs Futures and dynamically sizes leverage."""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from config.settings import Settings, settings
from database.db_manager import DatabaseManager
from engine.risk_engine import RiskEngine
from engine.schemas import ReflexDecisionSchema

logger = logging.getLogger(__name__)


class MarketRouter:
    """
    R15 Market Router Rules (in strict order):
    1) side == 'short' -> FUTURES only (no shorting on spot)
    2) ENABLE_FUTURES is false -> SPOT only
    3) Run RiskEngine.calculate_position_size for both:
       - spot_ok = notional >= spot_min and spot_cash >= notional
       - futures_ok = notional >= fut_min and liq_buffer satisfied and futures_cash >= margin
    4) If both valid: pick lower total cost (fee + funding + slippage); prefer SPOT if no leverage needed.
    5) If neither valid: skip trade and output reject_reason.
    """

    def __init__(
        self,
        db: DatabaseManager,
        custom_settings: Optional[Settings] = None,
    ) -> None:
        self.db = db
        self.cfg = custom_settings or settings
        self.risk_engine = RiskEngine(db=db, custom_settings=self.cfg)

    def route_market_and_size(
        self,
        decision: ReflexDecisionSchema,
        spot_free_usdt: float,
        futures_free_usdt: float,
        funding_rate_pct: float = 0.0,
    ) -> Dict[str, Any]:
        side = getattr(decision, "direction", getattr(decision, "side", "long"))
        is_short = str(side).lower() == "short"
        enable_futures = bool(getattr(self.cfg, "ENABLE_FUTURES", True))
        futures_only = bool(getattr(self.cfg, "FUTURES_ONLY_MODE", True))
        allow_spot_fallback = bool(getattr(self.cfg, "ALLOW_SPOT_FALLBACK", False))
        enable_short = bool(getattr(self.cfg, "ENABLE_SHORT", True))

        # Rule 1: Shorting requires ENABLE_FUTURES and ENABLE_SHORT
        if is_short and not enable_futures:
            return {
                "approved": False,
                "market_type": "NONE",
                "leverage": 1,
                "position_usd": 0.0,
                "margin_usd": 0.0,
                "router_reason": "Short signals not allowed when ENABLE_FUTURES is false (no spot shorting).",
            }
        if is_short and not enable_short:
            return {
                "approved": False,
                "market_type": "NONE",
                "leverage": 1,
                "position_usd": 0.0,
                "margin_usd": 0.0,
                "router_reason": "Short signals disabled (ENABLE_SHORT=false).",
            }

        # Check funding threshold on futures
        max_funding = float(getattr(self.cfg, "MAX_FUNDING_PCT", 0.05))
        funding_too_high = abs(funding_rate_pct) > max_funding

        # Sizing evaluations
        spot_res = None
        if not is_short and (not futures_only or allow_spot_fallback):
            spot_res = self.risk_engine.compute_position_size_usd(
                decision=decision,
                free_usdt=spot_free_usdt,
                trading_mode="SPOT",
            )

        futures_res = None
        if enable_futures and not funding_too_high:
            futures_res = self.risk_engine.compute_position_size_usd(
                decision=decision,
                free_usdt=spot_free_usdt,
                futures_free_usdt=futures_free_usdt,
                trading_mode="FUTURES",
            )

        spot_ok = spot_res is not None and spot_res.get("approved", False) and spot_free_usdt >= spot_res.get("position_usd", 0.0)
        fut_ok = futures_res is not None and futures_res.get("approved", False) and futures_free_usdt >= futures_res.get("margin_usd", 0.0)

        # If short only
        if is_short:
            if fut_ok:
                return {
                    "approved": True,
                    "market_type": "FUTURES",
                    "leverage": futures_res.get("leverage", 1),
                    "position_usd": futures_res["position_usd"],
                    "margin_usd": futures_res["margin_usd"],
                    "router_reason": "Short signal routed to USD-M Futures.",
                }
            return {
                "approved": False,
                "market_type": "FUTURES",
                "leverage": 1,
                "position_usd": 0.0,
                "margin_usd": 0.0,
                "router_reason": futures_res.get("reason", "Futures margin/risk checks rejected short."),
            }

        # If spot only
        if not enable_futures:
            if spot_ok:
                return {
                    "approved": True,
                    "market_type": "SPOT",
                    "leverage": 1,
                    "position_usd": spot_res["position_usd"],
                    "margin_usd": spot_res["position_usd"],
                    "router_reason": "Futures disabled; routed to Spot.",
                }
            return {
                "approved": False,
                "market_type": "SPOT",
                "leverage": 1,
                "position_usd": 0.0,
                "margin_usd": 0.0,
                "router_reason": spot_res.get("reason", "Spot risk constraints rejected position."),
            }

        # Both spot & futures are possible for long
        if spot_ok and fut_ok:
            # Rule 4: Pick lower cost, prefer SPOT if no leverage needed
            spot_fee = float(getattr(self.cfg, "SPOT_FEE_PCT", 0.10))
            fut_fee = float(getattr(self.cfg, "FUTURES_FEE_PCT", 0.05))
            cost_spot = spot_fee * 2.0
            cost_fut = fut_fee * 2.0 + abs(funding_rate_pct)

            if cost_spot <= cost_fut or spot_res["position_usd"] <= spot_free_usdt:
                return {
                    "approved": True,
                    "market_type": "SPOT",
                    "leverage": 1,
                    "position_usd": spot_res["position_usd"],
                    "margin_usd": spot_res["position_usd"],
                    "router_reason": "Both valid; Spot selected (unleveraged safe execution).",
                }
            else:
                return {
                    "approved": True,
                    "market_type": "FUTURES",
                    "leverage": futures_res.get("leverage", 1),
                    "position_usd": futures_res["position_usd"],
                    "margin_usd": futures_res["margin_usd"],
                    "router_reason": "Both valid; Futures selected (lower net cost/higher capital efficiency).",
                }

        if spot_ok:
            return {
                "approved": True,
                "market_type": "SPOT",
                "leverage": 1,
                "position_usd": spot_res["position_usd"],
                "margin_usd": spot_res["position_usd"],
                "router_reason": "Routed to Spot (Futures capital/margin criteria not met).",
            }

        if fut_ok:
            return {
                "approved": True,
                "market_type": "FUTURES",
                "leverage": futures_res.get("leverage", 1),
                "position_usd": futures_res["position_usd"],
                "margin_usd": futures_res["margin_usd"],
                "router_reason": "Routed to Futures (Spot cash insufficient).",
            }

        # Neither valid
        reason = spot_res.get("reason", "Spot checks failed") if spot_res else "Spot unavailable"
        if futures_res:
            reason += f" | Futures: {futures_res.get('reason')}"
        return {
            "approved": False,
            "market_type": "NONE",
            "leverage": 1,
            "position_usd": 0.0,
            "margin_usd": 0.0,
            "router_reason": f"Skipped trade: {reason}",
        }
