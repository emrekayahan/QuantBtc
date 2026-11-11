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


class PaperModeViolation(RuntimeError):
    """Raised when an attempt is made to execute a write operation to the exchange while in paper mode."""
    pass


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
            self.futures_client = None
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

            # Optional USD-M Futures client for auto-detecting Futures funding (>= $5.0 USDT)
            futures_config: Dict[str, Any] = {
                "enableRateLimit": True,
                "timeout": 10000,
                "options": {
                    "defaultType": "future",
                    "fetchMarkets": ["linear"],
                    "adjustForTimeDifference": True,
                    "recvWindow": 10000,
                    "fetchCurrencies": False,
                },
            }
            if self.cfg.has_valid_binance_keys:
                futures_config["apiKey"] = self.cfg.BINANCE_API_KEY
                futures_config["secret"] = self.cfg.BINANCE_API_SECRET
            self.futures_client = ccxt.binance(futures_config)
        self._futures_markets_loaded = False

    def _assert_live_or_raise(self, action: str) -> None:
        """Central gate ensuring no write command is ever sent to exchange in paper mode."""
        if getattr(self.cfg, "is_paper", True):
            msg = f"CRITICAL: Attempted exchange write action '{action}' while TRADING_MODE is paper!"
            logger.critical(msg)
            raise PaperModeViolation(msg)

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
        # If first call included cold DNS/TLS handshake (>120ms), take a warm measurement
        if (local_after - local_before) > 120:
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

    async def fetch_futures_balance(self) -> Dict[str, float]:
        """Safely query USD-M Futures USDT balance; returns 0.0 if Futures is unactivated or lacks permission."""
        if self.futures_client is None or not self.cfg.has_valid_binance_keys:
            return {"free": 0.0, "total": 0.0}
        try:
            bal = await self.futures_client.fetch_balance()
            free_u = float(bal.get("free", {}).get("USDT", 0.0) or 0.0)
            total_u = float(bal.get("total", {}).get("USDT", free_u) or free_u)
            return {"free": free_u, "total": total_u}
        except Exception as exc:
            logger.debug("USD-M Futures balance check skipped/unavailable: %s", exc)
            return {"free": 0.0, "total": 0.0}

    async def fetch_dual_wallet_balances(self) -> Dict[str, Any]:
        """
        Query both Spot and USD-M Futures wallets and auto-detect trading mode:
        - If futures_free_usdt >= FUTURES_MIN_USDT ($5.0): enable 'FUTURES' (2x-3x Isolated).
        - Otherwise: run in 'SPOT' mode using spot_free_usdt.
        """
        spot_bal = await self.fetch_balance()
        spot_free = float(spot_bal.get("free", {}).get("USDT", 0.0) or 0.0)
        spot_total = float(spot_bal.get("total", {}).get("USDT", spot_free) or spot_free)

        fut_bal = await self.fetch_futures_balance()
        fut_free = float(fut_bal.get("free", 0.0))
        fut_total = float(fut_bal.get("total", 0.0))

        # Check API key trading permissions (cached for 60s) so we only activate FUTURES if enableFutures is True
        now_s = int(time.time())
        last_perm_check = getattr(self, "_last_perm_check_s", 0)
        enable_futures_perm = getattr(self, "_enable_futures_perm", True)
        if (now_s - last_perm_check) >= 60 and hasattr(self.client, "sapi_get_account_apirestrictions"):
            try:
                restr = await self.client.sapi_get_account_apirestrictions()
                enable_futures_perm = bool(restr.get("enableFutures", False))
                self._enable_futures_perm = enable_futures_perm
                self._last_perm_check_s = now_s
            except Exception as exc:
                logger.debug("Could not query API restrictions: %s", exc)

        futures_min = float(getattr(self.cfg, "FUTURES_MIN_USDT", 5.0))
        active_mode = "FUTURES" if (fut_free >= futures_min and enable_futures_perm) else "SPOT"

        if self.db is not None:
            self.db.set_state("spot_free_usdt", f"{spot_free:.2f}")
            self.db.set_state("futures_free_usdt", f"{fut_free:.2f}")
            self.db.set_state("futures_total_usdt", f"{fut_total:.2f}")
            self.db.set_state("active_trading_mode", active_mode)
            self.db.set_state("futures_api_enabled", "true" if enable_futures_perm else "false")

        return {
            "spot_free_usdt": spot_free,
            "spot_total_usdt": spot_total,
            "futures_free_usdt": fut_free,
            "futures_total_usdt": fut_total,
            "active_mode": active_mode,
        }

    async def ensure_futures_markets_loaded(self, reload: bool = False) -> Dict[str, Any]:
        """Load USD-M Futures markets using public exchangeInfo without triggering private SAPI calls."""
        if self.futures_client is None or not hasattr(self.futures_client, "load_markets"):
            self._futures_markets_loaded = True
            return getattr(self.futures_client, "markets", None) or {}
        if not self._futures_markets_loaded or reload:
            saved_key = getattr(self.futures_client, "apiKey", None)
            saved_secret = getattr(self.futures_client, "secret", None)
            try:
                if hasattr(self.futures_client, "apiKey"):
                    self.futures_client.apiKey = ""
                    self.futures_client.secret = ""
                await self._with_backoff(lambda: self.futures_client.load_markets(reload))
                self._futures_markets_loaded = True
            finally:
                if saved_key is not None:
                    self.futures_client.apiKey = saved_key
                if saved_secret is not None:
                    self.futures_client.secret = saved_secret
        return self.futures_client.markets or {}

    def resolve_futures_symbol(self, symbol: str) -> str:
        """Map standard 'SOL/USDT' symbol to CCXT linear perpetual symbol 'SOL/USDT:USDT' if present."""
        if self.futures_client is not None and self.futures_client.markets:
            perp_sym = f"{symbol}:USDT" if ":" not in symbol else symbol
            if perp_sym in self.futures_client.markets:
                return perp_sym
        return symbol

    def futures_amount_to_precision(self, symbol: str, amount: float) -> float:
        """Format order quantity to Binance USD-M Futures LOT_SIZE precision."""
        try:
            if self.futures_client is not None and self.futures_client.markets:
                fut_sym = self.resolve_futures_symbol(symbol)
                if fut_sym in self.futures_client.markets:
                    return float(self.futures_client.amount_to_precision(fut_sym, amount))
        except Exception:
            pass
        return self.amount_to_precision(symbol, amount)

    def futures_price_to_precision(self, symbol: str, price: float) -> float:
        """Format order price to Binance USD-M Futures PRICE_FILTER precision."""
        try:
            if self.futures_client is not None and self.futures_client.markets:
                fut_sym = self.resolve_futures_symbol(symbol)
                if fut_sym in self.futures_client.markets:
                    return float(self.futures_client.price_to_precision(fut_sym, price))
        except Exception:
            pass
        return self.price_to_precision(symbol, price)

    async def configure_futures_isolated(self, symbol: str, leverage: int = 20) -> int:
        """Configure ISOLATED margin mode and target leverage (with automatic bracket fallback) for USD-M Futures."""
        self._assert_live_or_raise("configure_futures_isolated")
        if self.futures_client is None:
            return leverage
        await self.ensure_futures_markets_loaded()
        fut_symbol = self.resolve_futures_symbol(symbol)
        try:
            await self.futures_client.set_margin_mode("ISOLATED", fut_symbol)
        except Exception as exc:
            logger.debug("Futures set_margin_mode(ISOLATED, %s) info: %s", fut_symbol, exc)
        for cand_lev in (int(leverage), 15, 10, 5, 3):
            try:
                await self.futures_client.set_leverage(cand_lev, fut_symbol)
                return cand_lev
            except Exception as exc:
                logger.debug("Futures set_leverage(%d, %s) info: %s", cand_lev, fut_symbol, exc)
        return leverage

    async def create_futures_order(
        self,
        symbol: str,
        order_type: str,
        side: str,
        amount: float,
        price: Optional[float] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Place an order on USD-M Futures when FUTURES mode is active."""
        self._assert_live_or_raise(f"create_futures_order({symbol}, {order_type}, {side})")
        if self.futures_client is None:
            raise RuntimeError("Futures client is not initialized")
        await self.ensure_futures_markets_loaded()
        fut_symbol = self.resolve_futures_symbol(symbol)
        fmt_amount = self.futures_amount_to_precision(symbol, amount)
        fmt_price = self.futures_price_to_precision(symbol, price) if price is not None else None
        extra_params = dict(params or {})
        if "stopPrice" in extra_params and extra_params["stopPrice"] is not None:
            extra_params["stopPrice"] = self.futures_price_to_precision(
                symbol, float(extra_params["stopPrice"])
            )
        return await self.futures_client.create_order(
            fut_symbol, order_type, side, fmt_amount, fmt_price, extra_params
        )

    async def fetch_open_orders(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        await self.ensure_markets_loaded()
        return await self._with_backoff(lambda: self.client.fetch_open_orders(symbol), clear_auth_banner=True)

    async def fetch_order(self, order_id: str, symbol: str) -> Dict[str, Any]:
        await self.ensure_markets_loaded()
        return await self._with_backoff(lambda: self.client.fetch_order(order_id, symbol), clear_auth_banner=True)

    async def fetch_futures_order(self, order_id: str, symbol: str) -> Optional[Dict[str, Any]]:
        """Query USD-M Futures for a standard or conditional/algo Stop order by ID."""
        if self.futures_client is None:
            return None
        await self.ensure_futures_markets_loaded()
        fut_symbol = self.resolve_futures_symbol(symbol)
        # 1. Try conditional/algo stop order lookup first (STOP_MARKET orders use algoId)
        try:
            return await self.futures_client.fetch_order(
                order_id, fut_symbol, params={"stop": True}
            )
        except Exception:
            pass
        # 2. Fallback: scan recent conditional/algo stop orders for matching id
        if hasattr(self.futures_client, "fetch_orders"):
            try:
                stop_orders = await self.futures_client.fetch_orders(
                    fut_symbol, params={"stop": True}
                )
                for o in stop_orders:
                    if str(o.get("id")) == str(order_id):
                        return o
            except Exception:
                pass
        # 3. Fallback: standard order lookup
        try:
            return await self.futures_client.fetch_order(order_id, fut_symbol)
        except Exception as exc:
            logger.debug("Could not fetch Futures order %s on %s: %s", order_id, symbol, exc)
            return None

    async def fetch_active_futures_positions(self) -> Dict[str, Dict[str, Any]]:
        """Return dict of active USD-M Futures positions keyed by base symbol (e.g. 'ZEC/USDT')."""
        if self.futures_client is None or not self.cfg.has_valid_binance_keys:
            return {}
        try:
            await self.ensure_futures_markets_loaded()
            positions = await self.futures_client.fetch_positions()
            result: Dict[str, Dict[str, Any]] = {}
            for p in positions:
                contracts = abs(float(p.get("contracts") or 0.0))
                if contracts > 0.0:
                    raw_sym = str(p.get("symbol") or "")
                    clean_sym = raw_sym.split(":")[0]
                    result[clean_sym] = p
            return result
        except Exception as exc:
            logger.debug("Could not fetch Futures positions: %s", exc)
            return {}

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
        self._assert_live_or_raise(f"create_order({symbol}, {order_type}, {side})")
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
        self._assert_live_or_raise(f"create_market_sell_order({symbol})")
        await self.ensure_markets_loaded()
        fmt_amount = self.amount_to_precision(symbol, amount)
        return await self._with_backoff(
            lambda: self.client.create_order(symbol, "market", "sell", fmt_amount, None, params or {})
        )

    async def cancel_order(self, order_id: str, symbol: str) -> Dict[str, Any]:
        self._assert_live_or_raise(f"cancel_order({order_id}, {symbol})")
        return await self._with_backoff(lambda: self.client.cancel_order(order_id, symbol))

    async def cancel_all_orders(self, symbol: str) -> Any:
        self._assert_live_or_raise(f"cancel_all_orders({symbol})")
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
        if getattr(self, "futures_client", None) is not None and hasattr(self.futures_client, "close"):
            try:
                await self.futures_client.close()
            except Exception:
                pass
