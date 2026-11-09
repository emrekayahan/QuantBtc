"""FastAPI LAN/WAN Web Dashboard backend bound to 0.0.0.0:8000 with HTTP Basic Auth protection."""

from __future__ import annotations

import logging
import secrets
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd
from fastapi import Depends, FastAPI, HTTPException, Query, status
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles

from config.settings import get_settings, settings
from database.db_manager import DatabaseManager
from engine.exchange import BinanceExchangeWrapper
from engine.execution import ExecutionEngine
from engine.openrouter_pool import OpenRouterPool
from engine.risk_engine import RiskEngine
from engine.scanner import compute_ema

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"
security = HTTPBasic(auto_error=False)


def verify_dashboard_auth(
    credentials: Optional[HTTPBasicCredentials] = Depends(security),
) -> str:
    """Enforce constant-time HTTP Basic Authentication when DASHBOARD_PASSWORD is set."""
    cfg = get_settings()
    expected_user = (cfg.DASHBOARD_USERNAME or "").strip()
    expected_pass = (cfg.DASHBOARD_PASSWORD or "").strip()

    # If operator explicitly disables password by setting empty string, allow access
    if not expected_pass:
        return "anonymous"

    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required to access Quant Engine Dashboard",
            headers={"WWW-Authenticate": 'Basic realm="QuantBotLive Dashboard"'},
        )

    is_user_ok = secrets.compare_digest(credentials.username.encode("utf-8"), expected_user.encode("utf-8"))
    is_pass_ok = secrets.compare_digest(credentials.password.encode("utf-8"), expected_pass.encode("utf-8"))

    if not (is_user_ok and is_pass_ok):
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
    async def trigger_kill_switch(_user: str = Depends(verify_dashboard_auth)) -> JSONResponse:
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
    async def resume_system(_user: str = Depends(verify_dashboard_auth)) -> JSONResponse:
        db.set_system_halted(False, reason="Operator resumed system from Dashboard")
        return JSONResponse({"system_halted": False, "status": "ACTIVE"})

    @app.post("/api/positions/{position_id}/close")
    async def close_position_endpoint(
        position_id: str, _user: str = Depends(verify_dashboard_auth)
    ) -> JSONResponse:
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
