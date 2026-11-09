"""Unit tests for Phase 5: Overnight Brier Score self-tuning and LAN Dashboard FastAPI endpoints."""

from __future__ import annotations

import time
import pytest
from httpx import ASGITransport, AsyncClient

from dashboard.server import create_dashboard_app
from database.db_manager import DatabaseManager
from engine.risk_engine import RiskEngine
from review.overnight_review import OvernightBrierReviewer


@pytest.mark.asyncio
async def test_overnight_brier_calibration_raises_threshold_when_overconfident(tmp_path) -> None:
    """Verify BS > 0.25 raises MIN_CONFIDENCE_THRESHOLD by +0.03 and reduces MAX_POSITION_USD by 20%."""
    db = DatabaseManager(db_path=str(tmp_path / "brier_test.db"))
    try:
        now_ms = int(time.time() * 1000)
        # Open and close two losing high-confidence trades -> outcome = 0, confidence = 0.85
        # Brier Score = (0.85 - 0)^2 = 0.7225 > 0.25
        for idx in (1, 2):
            pid = f"POS_TEST_{idx}"
            db.open_position(
                {
                    "position_id": pid,
                    "symbol": f"COIN{idx}/USDT",
                    "entry_client_order_id": f"QB_{idx}",
                    "entry_price": 100.0,
                    "current_price": 98.0,
                    "invalidation_price": 98.2,
                    "take_profit_price": 104.5,
                    "quantity": 0.25,
                    "cost_usd": 25.0,
                    "direction_confidence": 0.85,
                    "opened_at_ms": now_ms - 3600_000,
                }
            )
            db.close_position(pid, exit_price=98.2, close_reason="STOP_LOSS")

        reviewer = OvernightBrierReviewer(db=db, openrouter_pool=None)
        res = await reviewer.run_overnight_review()

        assert res["sample_count"] == 2
        assert res["brier_score"] > 0.25
        assert res["min_confidence_threshold"] == pytest.approx(0.83, abs=1e-4)
        assert res["max_position_usd"] == pytest.approx(20.0, abs=1e-2)
    finally:
        db.close()


@pytest.mark.asyncio
async def test_dashboard_telemetry_and_kill_switch_endpoint(tmp_path) -> None:
    """Verify FastAPI LAN Dashboard enforces HTTP Basic Auth, serves HTML/telemetry, and triggers Kill-Switch."""
    from config.settings import get_settings

    cfg = get_settings()
    auth = (cfg.DASHBOARD_USERNAME, cfg.DASHBOARD_PASSWORD)

    db = DatabaseManager(db_path=str(tmp_path / "dash_test.db"))
    try:
        risk = RiskEngine(db=db)
        app = create_dashboard_app(db=db, risk_engine=risk)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            # 1. Unauthenticated access must be rejected with 401
            r_unauth = await client.get("/api/telemetry")
            assert r_unauth.status_code == 401

            # 2. Authenticated access succeeds
            r_index = await client.get("/", auth=auth)
            assert r_index.status_code == 200
            assert "PANIC STOP / FLATTEN ALL" in r_index.text

            r_tel = await client.get("/api/telemetry", auth=auth)
            assert r_tel.status_code == 200
            data = r_tel.json()
            assert data["kpis"]["system_status"] == "ACTIVE"

            r_kill = await client.post("/api/kill-switch", auth=auth)
            assert r_kill.status_code == 200
            assert r_kill.json()["system_halted"] is True
            assert db.is_system_halted() is True
    finally:
        db.close()
