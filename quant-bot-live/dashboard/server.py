"""FastAPI LAN/WAN Web Dashboard backend bound to 0.0.0.0:8000 with HTTP Basic Auth protection."""

from __future__ import annotations

import asyncio
import logging
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles

from config.settings import Settings, get_settings, settings
from database.db_manager import DatabaseManager
from engine.exchange import BinanceExchangeWrapper
from engine.execution import ExecutionEngine
from engine.openrouter_pool import OpenRouterPool
from engine.risk_engine import RiskEngine
from engine.scanner import compute_ema

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"
security = HTTPBasic(auto_error=False)

_failed_attempts: Dict[str, List[float]] = {}
_attempts_lock = threading.Lock()


def validate_dashboard_security_or_raise(cfg: Settings) -> None:
    """Strictly reject weak or default dashboard credentials upon startup."""
    user = (cfg.DASHBOARD_USERNAME or "").strip()
    pwd = (cfg.DASHBOARD_PASSWORD or "").strip()

    if cfg.DASHBOARD_HOST not in ("127.0.0.1", "localhost"):
        logger.warning(
            "SECURITY WARNING: Dashboard host is '%s' (external). "
            "HTTPS reverse proxy (e.g. Caddy/Nginx) is strongly required!",
            cfg.DASHBOARD_HOST,
        )

    if not pwd:
        raise ValueError("DASHBOARD_PASSWORD cannot be empty. Must be at least 16 characters.")
    if pwd.startswith("change_me"):
        raise ValueError("DASHBOARD_PASSWORD cannot use default 'change_me_*' placeholder.")
    if len(pwd) < 16:
        raise ValueError(f"DASHBOARD_PASSWORD is too short ({len(pwd)} chars). Minimum 16 characters required.")
    if user.lower() == "admin":
        raise ValueError("DASHBOARD_USERNAME cannot be 'admin'. Choose a dedicated non-default username.")


async def verify_dashboard_auth(
    request: Request,
    credentials: Optional[HTTPBasicCredentials] = Depends(security),
) -> str:
    """Enforce constant-time HTTP Basic Authentication with rate limiting & anti-bruteforce delay."""
    cfg = get_settings()
    expected_user = (cfg.DASHBOARD_USERNAME or "").strip()
    expected_pass = (cfg.DASHBOARD_PASSWORD or "").strip()

    # If operator explicitly disables password by setting empty string, allow access
    if not expected_pass:
        return "anonymous"

    client_ip = request.client.host if request.client else "127.0.0.1"
    now = time.time()
    with _attempts_lock:
        timestamps = [t for t in _failed_attempts.get(client_ip, []) if now - t < 60.0]
        _failed_attempts[client_ip] = timestamps
        if len(timestamps) >= 5:
            logger.warning("Rate limit exceeded for dashboard auth from IP %s", client_ip)
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many failed authentication attempts. Please try again in 60 seconds.",
            )

    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required to access Quant Engine Dashboard",
            headers={"WWW-Authenticate": 'Basic realm="QuantBotLive Dashboard"'},
        )

    is_user_ok = secrets.compare_digest(credentials.username.encode("utf-8"), expected_user.encode("utf-8"))
    is_pass_ok = secrets.compare_digest(credentials.password.encode("utf-8"), expected_pass.encode("utf-8"))

    if not (is_user_ok and is_pass_ok):
        with _attempts_lock:
            _failed_attempts.setdefault(client_ip, []).append(now)
        logger.warning(
            "Failed dashboard login attempt from IP %s for username '%s'",
            client_ip,
            credentials.username,
        )
        await asyncio.sleep(0.5)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid dashboard credentials",
            headers={"WWW-Authenticate": 'Basic realm="QuantBotLive Dashboard"'},
        )
    return credentials.username


def create_dashboard_app(
    db: DatabaseManager,
    exchange: Optional[BinanceExchangeWrapper] = None,
    risk_engine: Optional[RiskEngine] = None,
    execution_engine: Optional[ExecutionEngine] = None,
    openrouter_pool: Optional[OpenRouterPool] = None,
) -> FastAPI:
    """Create and configure the FastAPI LAN/WAN Dashboard application."""
    app = FastAPI(
        title="Autonomous Quantitative Trading Engine - Dashboard",
        version="3.0.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.get("/api/health")
    async def healthcheck() -> JSONResponse:
        return JSONResponse({"status": "ok", "engine": "quant-bot-live-v3"})

    @app.get("/")
    async def serve_index(_user: str = Depends(verify_dashboard_auth)) -> FileResponse:
        return FileResponse(str(STATIC_DIR / "index.html"))

    @app.get("/api/telemetry")
    async def get_telemetry(_user: str = Depends(verify_dashboard_auth)) -> JSONResponse:
        sys_state = db.get_all_system_state()
        open_positions = db.get_open_positions()
        watchlist = db.get_watchlist()
        latest_brier = db.get_latest_brier_calibration()
        or_telemetry = (
            openrouter_pool.get_telemetry()
            if openrouter_pool is not None
            else {"configured_keys": 0, "active_key_index": -1, "models": [], "keys": []}
        )

        total_equity = float(sys_state.get("total_equity_usd", "0.0") or 0.0)
        free_usdt = float(sys_state.get("free_usdt", "0.0") or 0.0)
        daily_pnl_usd = float(sys_state.get("daily_realized_pnl_usd", "0.0") or 0.0)
        daily_pnl_pct = (daily_pnl_usd / total_equity * 100.0) if total_equity > 0.0 else 0.0
        drawdown_pct = float(sys_state.get("current_drawdown_pct", "0.0") or 0.0)
        halted = sys_state.get("system_halted", "false").lower() in ("true", "1", "yes")
        entries_blocked = sys_state.get("entries_blocked", "false").lower() in ("true", "1", "yes")
        reconcile_ok = sys_state.get("reconcile_ok", "true").lower() in ("true", "1", "yes")
        last_reconcile_ts = int(sys_state.get("last_reconcile_ts", "0") or 0)

        return JSONResponse(
            {
                "kpis": {
                    "total_equity_usd": round(total_equity, 2),
                    "free_usdt": round(free_usdt, 2),
                    "daily_realized_pnl_usd": round(daily_pnl_usd, 2),
                    "daily_realized_pnl_pct": round(daily_pnl_pct, 2),
                    "current_drawdown_pct": round(drawdown_pct, 2),
                    "macro_regime": sys_state.get("macro_regime", "mean_reverting").upper(),
                    "macro_commentary": sys_state.get("macro_commentary", ""),
                    "system_status": "HALTED" if halted else "ACTIVE",
                    "halt_reason": sys_state.get("halt_reason", ""),
                    "entries_blocked": entries_blocked,
                    "reconcile_ok": reconcile_ok,
                    "last_reconcile_ts": last_reconcile_ts,
                    "auth_error_banner": sys_state.get("auth_error_banner", ""),
                    "clock_drift_ms": int(sys_state.get("clock_drift_ms", "0") or 0),
                    "min_confidence_threshold": float(
                        sys_state.get(
                            "min_confidence_threshold", str(settings.MIN_CONFIDENCE_THRESHOLD)
                        )
                    ),
                    "max_position_usd": float(
                        sys_state.get("max_position_usd", str(settings.MAX_POSITION_USD))
                    ),
                    "trading_mode": str(settings.TRADING_MODE).lower(),
                    "is_paper": settings.is_paper,
                },
                "open_positions": open_positions,
                "watchlist": watchlist,
                "openrouter": or_telemetry,
                "brier": latest_brier
                or {
                    "review_date": "Pending 00:05 UTC",
                    "sample_count": 0,
                    "brier_score": 0.0,
                    "min_confidence_threshold": settings.MIN_CONFIDENCE_THRESHOLD,
                    "max_position_usd": settings.MAX_POSITION_USD,
                    "notes": "No overnight Brier review recorded yet.",
                },
            }
        )

    @app.get("/api/chart")
    async def get_chart_data(
        symbol: str = Query(default="BTC/USDT", description="Trading pair symbol, e.g. BTC/USDT"),
        _user: str = Depends(verify_dashboard_auth),
    ) -> JSONResponse:
        candles_out: List[Dict[str, Any]] = []
        ema200_out: List[Dict[str, Any]] = []

        if exchange is not None:
            try:
                raw = await exchange.fetch_ohlcv(symbol, timeframe=settings.TIMEFRAME, limit=220)
                if len(raw) >= 2:
                    df = pd.DataFrame(
                        raw, columns=["timestamp", "open", "high", "low", "close", "volume"]
                    ).iloc[:-1]
                    ema200_s = compute_ema(df["close"].astype(float), min(200, len(df)))
                    for idx, row in df.iterrows():
                        sec = int(row["timestamp"]) // 1000
                        candles_out.append(
                            {
                                "time": sec,
                                "open": float(row["open"]),
                                "high": float(row["high"]),
                                "low": float(row["low"]),
                                "close": float(row["close"]),
                            }
                        )
                        ema200_out.append({"time": sec, "value": float(ema200_s.loc[idx])})
            except Exception as exc:
                logger.warning("Chart fetch_ohlcv failed for %s: %s", symbol, exc)

        open_positions = [p for p in db.get_open_positions() if p["symbol"] == symbol]
        return JSONResponse(
            {
                "symbol": symbol,
                "timeframe": settings.TIMEFRAME,
                "candles": candles_out,
                "ema200": ema200_out,
                "positions": open_positions,
            }
        )

    @app.post("/api/kill-switch")
    async def trigger_kill_switch(
        x_confirm: Optional[str] = Header(None, alias="X-Confirm"),
        _user: str = Depends(verify_dashboard_auth),
    ) -> JSONResponse:
        if not x_confirm or x_confirm.strip().lower() != "true":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Missing or invalid confirmation header 'X-Confirm: true'",
            )
        if risk_engine is None:
            db.set_system_halted(True, reason="Kill-Switch triggered via Dashboard")
            return JSONResponse(
                {
                    "system_halted": True,
                    "reason": "Kill-Switch triggered via Dashboard",
                    "cancelled_orders": 0,
                    "flattened_positions": 0,
                    "errors": [],
                }
            )
        res = await risk_engine.trigger_emergency_kill_switch(
            reason="Operator clicked PANIC STOP / FLATTEN ALL on Dashboard"
        )
        return JSONResponse(res)

    @app.post("/api/resume")
    async def resume_system(
        x_confirm: Optional[str] = Header(None, alias="X-Confirm"),
        _user: str = Depends(verify_dashboard_auth),
    ) -> JSONResponse:
        if not x_confirm or x_confirm.strip().lower() != "true":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Missing or invalid confirmation header 'X-Confirm: true'",
            )
        db.set_system_halted(False, reason="Operator resumed system from Dashboard")
        return JSONResponse({"system_halted": False, "status": "ACTIVE"})

    @app.post("/api/positions/{position_id}/close")
    async def close_position_endpoint(
        position_id: str,
        x_confirm: Optional[str] = Header(None, alias="X-Confirm"),
        _user: str = Depends(verify_dashboard_auth),
    ) -> JSONResponse:
        if not x_confirm or x_confirm.strip().lower() != "true":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Missing or invalid confirmation header 'X-Confirm: true'",
            )
        if execution_engine is None:
            closed = db.close_position(
                position_id=position_id,
                exit_price=0.0,
                close_reason="MANUAL_DASHBOARD_CLOSE",
            )
            if not closed:
                raise HTTPException(status_code=404, detail=f"Position {position_id} not found")
            return JSONResponse({"closed": True, "position": closed})

        res = await execution_engine.close_single_position_market(
            position_id=position_id, reason="MANUAL_DASHBOARD_MARKET_CLOSE"
        )
        if not res.get("closed"):
            raise HTTPException(
                status_code=404, detail=res.get("reason", "Could not close position")
            )
        return JSONResponse(res)

    return app
