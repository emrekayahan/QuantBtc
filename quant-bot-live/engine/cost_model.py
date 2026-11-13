"""K4: CostModel (maker/taker fees, BNB discount, funding, slippage) & trade discipline."""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from config.settings import Settings, settings

logger = logging.getLogger("engine.cost_model")


class CostModel:
    """Computes realistic round-trip trading costs in R-multiples and enforces cost & discipline gates."""

    def __init__(self, custom_settings: Optional[Settings] = None) -> None:
        self.cfg = custom_settings or settings

    def get_fee_rates_pct(
        self,
        market_type: str = "FUTURES",
        bnb_discount: bool = False,
        override_maker_pct: Optional[float] = None,
        override_taker_pct: Optional[float] = None,
    ) -> Dict[str, float]:
        """Return maker and taker fee percentages (in %, e.g. 0.02 for 0.02%)."""
        is_fut = str(market_type).upper() == "FUTURES"
        if is_fut:
            maker = float(override_maker_pct if override_maker_pct is not None else self.cfg.FUTURES_FEE_MAKER_PCT)
            taker = float(override_taker_pct if override_taker_pct is not None else self.cfg.FUTURES_FEE_TAKER_PCT)
            disc = 0.90 if bnb_discount else 1.0  # 10% BNB discount on USD-M futures
        else:
            spot_fee = float(self.cfg.SPOT_FEE_PCT)
            maker = float(override_maker_pct if override_maker_pct is not None else spot_fee)
            taker = float(override_taker_pct if override_taker_pct is not None else spot_fee)
            disc = 0.75 if bnb_discount else 1.0  # 25% BNB discount on spot
        return {"maker_pct": maker * disc, "taker_pct": taker * disc}

    async def fetch_actual_fee_rates(
        self,
        exchange: Any,
        symbol: str,
        market_type: str = "FUTURES",
        bnb_discount: bool = False,
    ) -> Dict[str, float]:
        """Query live fee tier via CCXT fetch_trading_fee with safe config fallback."""
        try:
            is_fut = str(market_type).upper() == "FUTURES"
            client = getattr(exchange, "futures_client", None) if is_fut else getattr(exchange, "client", None)
            if client is not None and hasattr(client, "fetch_trading_fee"):
                sym = f"{symbol}:USDT" if (is_fut and ":" not in symbol) else symbol
                fee_info = await client.fetch_trading_fee(sym)
                if isinstance(fee_info, dict) and fee_info.get("maker") is not None and fee_info.get("taker") is not None:
                    return self.get_fee_rates_pct(
                        market_type=market_type,
                        bnb_discount=bnb_discount,
                        override_maker_pct=float(fee_info["maker"]) * 100.0,
                        override_taker_pct=float(fee_info["taker"]) * 100.0,
                    )
        except Exception as exc:
            logger.debug("fetch_trading_fee fallback for %s: %s", symbol, exc)
        return self.get_fee_rates_pct(market_type=market_type, bnb_discount=bnb_discount)

    @staticmethod
    def estimate_funding_cost_pct(
        funding_rate_8h: float,
        hold_hours: float,
        side: str,
    ) -> float:
        """
        Estimate funding cost in % of notional over hold_hours.
        Positive return = cost paid; negative return = funding rebate received.
        funding_rate_8h is fractional (e.g. 0.0003 = 0.03% per 8h).
        """
        if hold_hours <= 0.0 or funding_rate_8h == 0.0:
            return 0.0
        intervals = hold_hours / 8.0
        rate_pct = float(funding_rate_8h) * 100.0
        signed_pct = rate_pct * intervals
        return signed_pct if str(side).lower() == "long" else -signed_pct

    def compute_cost_r(
        self,
        entry: float,
        stop: float,
        side: str,
        market_type: str = "FUTURES",
        entry_maker: bool = True,
        exit_taker: bool = True,
        slippage_pct: Optional[float] = None,
        funding_rate_8h: float = 0.0,
        expected_hold_hours: float = 4.0,
        bnb_discount: bool = False,
        override_maker_pct: Optional[float] = None,
        override_taker_pct: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Compute full cost breakdown and cost_R = total_cost_pct / stop_pct."""
        if entry <= 0.0:
            raise ValueError("entry must be positive")
        raw_stop_pct = abs(entry - stop) / entry * 100.0
        min_stop_pct = float(self.cfg.MIN_STOP_PCT)
        effective_stop_pct = max(raw_stop_pct, 1e-6)

        fees = self.get_fee_rates_pct(
            market_type=market_type,
            bnb_discount=bnb_discount,
            override_maker_pct=override_maker_pct,
            override_taker_pct=override_taker_pct,
        )
        entry_fee_pct = fees["maker_pct"] if entry_maker else fees["taker_pct"]
        exit_fee_pct = fees["taker_pct"] if exit_taker else fees["maker_pct"]

        slip_pct = float(slippage_pct if slippage_pct is not None else self.cfg.SLIPPAGE_PCT)
        # Maker entry avoids entry slippage; taker exit pays slippage
        total_slip_pct = (slip_pct if exit_taker else 0.0) + (0.0 if entry_maker else slip_pct)

        funding_pct = (
            self.estimate_funding_cost_pct(funding_rate_8h, expected_hold_hours, side)
            if str(market_type).upper() == "FUTURES"
            else 0.0
        )

        total_cost_pct = entry_fee_pct + exit_fee_pct + total_slip_pct + funding_pct
        cost_r = total_cost_pct / effective_stop_pct
        max_cost_r = float(self.cfg.MAX_COST_R)
        passes_min_stop = raw_stop_pct >= min_stop_pct
        passes_cost_gate = passes_min_stop and (cost_r <= max_cost_r)

        return {
            "entry_fee_pct": round(entry_fee_pct, 6),
            "exit_fee_pct": round(exit_fee_pct, 6),
            "slippage_pct": round(total_slip_pct, 6),
            "funding_pct": round(funding_pct, 6),
            "total_cost_pct": round(total_cost_pct, 6),
            "stop_pct": round(raw_stop_pct, 6),
            "cost_r": round(cost_r, 6),
            "passes_min_stop": passes_min_stop,
            "passes_cost_gate": passes_cost_gate,
        }

    @staticmethod
    def expected_net_r(posterior_mean_r: float, cost_r: float) -> float:
        """Expected net R after costs."""
        return float(posterior_mean_r) - float(cost_r)

    def is_symbol_in_cooldown(
        self,
        last_sl_ms: Optional[int],
        now_ms: int,
        cooldown_hours: Optional[float] = None,
    ) -> bool:
        """Return True if symbol experienced an SL within SYMBOL_SL_COOLDOWN_HOURS."""
        if not last_sl_ms or last_sl_ms <= 0:
            return False
        hours = float(cooldown_hours if cooldown_hours is not None else self.cfg.SYMBOL_SL_COOLDOWN_HOURS)
        return (now_ms - int(last_sl_ms)) < int(hours * 3600 * 1000)

    def prioritize_signals(
        self,
        candidates: List[Dict[str, Any]],
        max_trades_remaining: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Filter expected_net_r > 0 and sort descending, capped by MAX_TRADES_PER_DAY / remaining."""
        cap = int(max_trades_remaining if max_trades_remaining is not None else self.cfg.MAX_TRADES_PER_DAY)
        if cap <= 0:
            return []
        positive = [c for c in candidates if float(c.get("expected_net_r", 0.0)) > 0.0]
        positive.sort(key=lambda c: float(c.get("expected_net_r", 0.0)), reverse=True)
        return positive[:cap]
