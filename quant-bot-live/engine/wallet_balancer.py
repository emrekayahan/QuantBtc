"""K5: WalletBalancer — detects Spot/Futures capital split and performs safe Universal Transfer."""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from config.settings import Settings, settings
from database.db_manager import DatabaseManager

logger = logging.getLogger("engine.wallet_balancer")


class WalletBalancer:
    """Monitors Spot vs Futures USDT split and optionally transfers when flat & authorized."""

    def __init__(
        self,
        db: DatabaseManager,
        exchange: Optional[Any] = None,
        custom_settings: Optional[Settings] = None,
    ) -> None:
        self.db = db
        self.exchange = exchange
        self.cfg = custom_settings or settings

    async def check_and_balance(
        self,
        spot_free_usdt: float,
        futures_free_usdt: float,
        has_open_positions: bool,
        has_open_orders: bool,
    ) -> Dict[str, Any]:
        total_usdt = float(spot_free_usdt) + float(futures_free_usdt)
        target_pct = float(self.cfg.FUTURES_WALLET_TARGET_PCT)
        min_transfer = float(self.cfg.MIN_TRANSFER_USD)
        auto_transfer = bool(self.cfg.AUTO_TRANSFER)

        target_futures = total_usdt * (target_pct / 100.0)
        needed = max(0.0, min(float(spot_free_usdt), target_futures - float(futures_free_usdt)))

        wallet_split_warning = needed >= min_transfer
        self.db.set_state("wallet_split_warning", "true" if wallet_split_warning else "false")

        if not wallet_split_warning:
            return {"wallet_split_warning": False, "transferred_usd": 0.0, "reason": "balanced"}

        if not auto_transfer:
            return {
                "wallet_split_warning": True,
                "transferred_usd": 0.0,
                "needed_usd": round(needed, 2),
                "reason": "AUTO_TRANSFER=false (manual Spot->Futures transfer recommended)",
            }

        if has_open_positions or has_open_orders:
            return {
                "wallet_split_warning": True,
                "transferred_usd": 0.0,
                "reason": "skipped_open_positions_or_orders",
            }

        if self.exchange is None or not hasattr(self.exchange, "transfer_spot_to_futures"):
            return {
                "wallet_split_warning": True,
                "transferred_usd": 0.0,
                "reason": "transfer_method_unavailable",
            }

        await self.exchange.transfer_spot_to_futures(round(needed, 2))
        self.db.set_state("wallet_split_warning", "false")
        logger.info("WalletBalancer transferred %.2f USDT from Spot to Futures.", needed)
        return {
            "wallet_split_warning": False,
            "transferred_usd": round(needed, 2),
            "reason": "transferred",
        }
