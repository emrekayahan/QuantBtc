"""Async CCXT Binance Spot wrapper with precision formatting, clock drift checks, and exponential backoff."""

from __future__ import annotations

import asyncio
import logging
import time
from decimal import Decimal, ROUND_DOWN
from typing import Any, Callable, Dict, List, Optional

import ccxt.async_support as ccxt

from config.settings import Settings, settings
from database.db_manager import DatabaseManager

logger = logging.getLogger(__name__)


class BinanceExchangeWrapper:
    """Production async CCXT Binance Spot wrapper."""

    def __init__(
        self,
        db: Optional[DatabaseManager] = None,
        custom_settings: Optional[Settings] = None,
        ccxt_client: Optional[Any] = None,
    ) -> None:
        self.cfg = custom_settings or settings
        self.db = db
        self._markets_loaded = False

        if ccxt_client is not None:
            self.client = ccxt_client
        else:
            exchange_config: Dict[str, Any] = {
                "enableRateLimit": True,
                "timeout": 10000,
                "options": {
                    "defaultType": "spot",
                    "fetchMarkets": ["spot"],
                    "adjustForTimeDifference": True,
                    "recvWindow": 10000,
                    "fetchCurrencies": False,
                },
            }
            if self.cfg.has_valid_binance_keys:
                exchange_config["apiKey"] = self.cfg.BINANCE_API_KEY
                exchange_config["secret"] = self.cfg.BINANCE_API_SECRET

            self.client = ccxt.binance(exchange_config)
            # Route public market data (time, tickers, OHLCV, orderbook) through data-api.binance.vision
            # so US-based Cloud VMs do not hit HTTP 451 on public market data endpoints
            if hasattr(self.client, "urls") and "api" in self.client.urls:
                self.client.urls["api"]["public"] = "https://data-api.binance.vision/api/v3"

    async def _with_backoff(self, coro_factory: Callable[[], Any], max_retries: int = 3, clear_auth_banner: bool = False) -> Any:
        delay = 0.5
        for attempt in range(1, max_retries + 1):
            try:
                res = await coro_factory()
                if clear_auth_banner and self.db is not None:
                    self.db.set_state("auth_error_banner", "")
                return res
            except ccxt.AuthenticationError as exc:
                msg = f"Binance Authentication Error (Check API Key / Secret): {exc}"
                logger.error(msg)
                if self.db is not None:
                    self.db.set_state("auth_error_banner", msg)
                raise
            except (ccxt.RequestTimeout, ccxt.NetworkError, ccxt.ExchangeNotAvailable) as exc:
                err_str = str(exc)
                # HTTP 451 is a regional restriction from Binance on US datacenter IPs; do not waste retries
                if " 451 " in err_str or "restricted location" in err_str:
                    msg = (
                        "Binance HTTP 451: Private trading API blocked on US datacenter IP (us-central1). "
                        "Public market data works via data-api.binance.vision; for live order execution use an EU/Asia VM."
                    )
                    logger.warning(msg)
                    if self.db is not None:
                        self.db.set_state("auth_error_banner", msg)
                    raise
                if attempt >= max_retries:
                    logger.error("Binance network operation failed after %d attempts: %s", attempt, exc)
                    raise
                logger.warning(
                    "Binance transient network error (attempt %d/%d): %s. Retrying in %.2fs",
                    attempt,
                    max_retries,
                    exc,
                    delay,
                )
                await asyncio.sleep(delay)
                delay *= 2.0

    async def ensure_markets_loaded(self, reload: bool = False) -> Dict[str, Any]:
        if not hasattr(self.client, "load_markets"):
            self._markets_loaded = True
            return getattr(self.client, "markets", None) or {}
        if not self._markets_loaded or reload:
            # Temporarily clear apiKey during load_markets so CCXT only calls public /exchangeInfo
            # on data-api.binance.vision instead of private /sapi/v1/capital/config/getall
            saved_key = getattr(self.client, "apiKey", None)
            saved_secret = getattr(self.client, "secret", None)
            try:
                if hasattr(self.client, "apiKey"):
                    self.client.apiKey = ""
                    self.client.secret = ""
                await self._with_backoff(lambda: self.client.load_markets(reload))
                self._markets_loaded = True
            finally:
                if saved_key is not None:
                    self.client.apiKey = saved_key
                if saved_secret is not None:
                    self.client.secret = saved_secret
        return self.client.markets or {}

    async def verify_clock_drift(self) -> int:
        """Check clock drift between local Ubuntu server and Binance server time in milliseconds."""
        local_before = int(time.time() * 1000)
        server_time = await self._with_backoff(lambda: self.client.fetch_time())
        local_after = int(time.time() * 1000)
        local_mid = (local_before + local_after) // 2
        drift_ms = int(server_time) - local_mid
        if self.db is not None:
            self.db.set_state("clock_drift_ms", str(drift_ms))
        if abs(drift_ms) > 1000:
            logger.warning("Clock drift exceeds 1000ms (%dms). Adjusting time difference.", drift_ms)
            if hasattr(self.client, "load_time_difference"):
                await self._with_backoff(lambda: self.client.load_time_difference())
        return drift_ms

    def amount_to_precision(self, symbol: str, amount: float) -> float:
        """Format order quantity to Binance LOT_SIZE step precision."""
        try:
            if self._markets_loaded and symbol in (self.client.markets or {}):
                formatted = self.client.amount_to_precision(symbol, amount)
                return float(formatted)
        except Exception:
            pass
        return float(Decimal(str(amount)).quantize(Decimal("0.000001"), rounding=ROUND_DOWN))

    def price_to_precision(self, symbol: str, price: float) -> float:
        """Format order price to Binance PRICE_FILTER tick precision."""
        try:
            if self._markets_loaded and symbol in (self.client.markets or {}):
                formatted = self.client.price_to_precision(symbol, price)
                return float(formatted)
        except Exception:
            pass
        return float(Decimal(str(price)).quantize(Decimal("0.0001"), rounding=ROUND_DOWN))

    def get_min_notional(self, symbol: str, default_min_usd: float = 10.0) -> float:
        """Retrieve Binance minimum notional cost limit for a symbol."""
        try:
            markets = self.client.markets or {}
            if symbol in markets:
                cost_limits = markets[symbol].get("limits", {}).get("cost", {})
                min_cost = cost_limits.get("min")
                if min_cost is not None and float(min_cost) > 0.0:
                    return float(min_cost)
        except Exception:
            pass
        return default_min_usd

    async def fetch_tickers(self) -> Dict[str, Any]:
        await self.ensure_markets_loaded()
        return await self._with_backoff(lambda: self.client.fetch_tickers())

    async def fetch_ohlcv(self, symbol: str, timeframe: str = "15m", limit: int = 250) -> List[List[Any]]:
        await self.ensure_markets_loaded()
        return await self._with_backoff(
            lambda: self.client.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        )

    async def fetch_order_book(self, symbol: str, limit: int = 10) -> Dict[str, Any]:
        await self.ensure_markets_loaded()
        return await self._with_backoff(lambda: self.client.fetch_order_book(symbol, limit=limit))

    async def fetch_balance(self) -> Dict[str, Any]:
        await self.ensure_markets_loaded()
        return await self._with_backoff(lambda: self.client.fetch_balance(), clear_auth_banner=True)

    async def fetch_open_orders(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        await self.ensure_markets_loaded()
        return await self._with_backoff(lambda: self.client.fetch_open_orders(symbol), clear_auth_banner=True)

    async def fetch_order(self, order_id: str, symbol: str) -> Dict[str, Any]:
        await self.ensure_markets_loaded()
        return await self._with_backoff(lambda: self.client.fetch_order(order_id, symbol), clear_auth_banner=True)

    async def fetch_order_by_client_order_id(
        self, client_order_id: str, symbol: str
    ) -> Optional[Dict[str, Any]]:
        """Query Binance Spot for an order matching newClientOrderId / origClientOrderId."""
        try:
            open_orders = await self.fetch_open_orders(symbol)
            for order in open_orders:
                cid = order.get("clientOrderId") or order.get("info", {}).get("clientOrderId")
                if cid == client_order_id:
                    return order
            if hasattr(self.client, "fetch_orders"):
                recent_orders = await self._with_backoff(
                    lambda: self.client.fetch_orders(symbol, limit=25)
                )
                for order in recent_orders:
                    cid = order.get("clientOrderId") or order.get("info", {}).get("clientOrderId")
                    if cid == client_order_id:
                        return order
        except Exception as exc:
            logger.warning(
                "Could not query order by clientOrderId=%s on %s: %s",
                client_order_id,
                symbol,
                exc,
            )
        return None

    async def create_order(
        self,
        symbol: str,
        order_type: str,
        side: str,
        amount: float,
        price: Optional[float] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        await self.ensure_markets_loaded()
        fmt_amount = self.amount_to_precision(symbol, amount)
        fmt_price = self.price_to_precision(symbol, price) if price is not None else None
        extra_params = dict(params or {})
        if "stopPrice" in extra_params and extra_params["stopPrice"] is not None:
            extra_params["stopPrice"] = self.price_to_precision(
                symbol, float(extra_params["stopPrice"])
            )
        # Single direct call here so ExecutionEngine controls idempotent timeout lookup before retrying
        return await self.client.create_order(
            symbol, order_type, side, fmt_amount, fmt_price, extra_params
        )

    async def create_market_sell_order(
        self, symbol: str, amount: float, params: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        await self.ensure_markets_loaded()
        fmt_amount = self.amount_to_precision(symbol, amount)
        return await self._with_backoff(
            lambda: self.client.create_order(symbol, "market", "sell", fmt_amount, None, params or {})
        )

    async def cancel_order(self, order_id: str, symbol: str) -> Dict[str, Any]:
        return await self._with_backoff(lambda: self.client.cancel_order(order_id, symbol))

    async def cancel_all_orders(self, symbol: str) -> Any:
        open_orders = await self.fetch_open_orders(symbol)
        cancelled = []
        for o in open_orders:
            oid = str(o["id"])
            res = await self.cancel_order(oid, symbol)
            cancelled.append(res)
        return cancelled

    async def close(self) -> None:
        if hasattr(self.client, "close"):
            await self.client.close()
