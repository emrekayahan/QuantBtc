"""Unit tests for Phase 1: Multi-key OpenRouter Pool rotation and HTTP 429 / timeout fallback."""

from __future__ import annotations

from typing import List
import httpx
import pytest

from config.settings import Settings
from database.db_manager import DatabaseManager
from engine.openrouter_pool import OpenRouterPool


def make_test_settings() -> Settings:
    return Settings(
        GEMINI_API_KEY="",
        OPENROUTER_API_KEY_1="sk-or-v1-test_key_alpha_111111",
        OPENROUTER_API_KEY_2="sk-or-v1-test_key_beta_222222",
        OPENROUTER_API_KEY_3="sk-or-v1-test_key_gamma_333333",
        OPENROUTER_MODELS="deepseek/deepseek-r1:free,qwen/qwen3-235b-a22b:free",
        OPENROUTER_TIMEOUT_SECONDS=2.0,
    )


@pytest.mark.asyncio
async def test_3_key_rotation_on_429_and_success_on_third_key() -> None:
    """Verify Key #1 (429) -> Key #2 (503) -> Key #3 (200 OK) rotation."""
    seen_auth_headers: List[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        auth = request.headers.get("Authorization", "")
        seen_auth_headers.append(auth)
        if len(seen_auth_headers) == 1:
            return httpx.Response(429, json={"error": "rate limited"})
        if len(seen_auth_headers) == 2:
            return httpx.Response(503, json={"error": "upstream unavailable"})
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": "Macro regime confirmed as trending with strong breadth."}}
                ]
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        pool = OpenRouterPool(custom_settings=make_test_settings(), http_client=client)
        result = await pool.complete_with_fallback(
            prompt="Analyze BTC/ETH regime.",
            deterministic_fallback="DETERMINISTIC_FALLBACK_TEXT",
        )

    assert result["source"] == "openrouter"
    assert result["key_index"] == 2
    assert result["model"] == "deepseek/deepseek-r1:free"
    assert "Macro regime confirmed" in result["content"]
    assert seen_auth_headers == [
        "Bearer sk-or-v1-test_key_alpha_111111",
        "Bearer sk-or-v1-test_key_beta_222222",
        "Bearer sk-or-v1-test_key_gamma_333333",
    ]
    telemetry = pool.get_telemetry()
    assert telemetry["keys"][0]["rate_limits"] == 1
    assert telemetry["keys"][1]["failures"] == 1
    assert telemetry["keys"][2]["status"] == "healthy"


@pytest.mark.asyncio
async def test_exhausted_keys_and_models_returns_deterministic_fallback() -> None:
    """Verify that if all 3 keys across all models return 429 or timeout, deterministic fallback is returned cleanly."""
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count % 2 == 1:
            return httpx.Response(429, json={"error": "rate limit exceeded"})
        raise httpx.ReadTimeout("Simulated timeout")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        pool = OpenRouterPool(custom_settings=make_test_settings(), http_client=client)
        result = await pool.complete_with_fallback(
            prompt="Generate overnight review.",
            deterministic_fallback=lambda: "PURE_MATH_FALLBACK: Brier=0.14, Regime=trending",
        )

    # 3 keys * 2 models = 6 total attempts before fallback
    assert call_count == 6
    assert result["source"] == "deterministic_fallback"
    assert result["content"] == "PURE_MATH_FALLBACK: Brier=0.14, Regime=trending"
    assert result["key_index"] is None


def test_duckdb_manager_crud_operations(tmp_path) -> None:
    """Verify thread-safe DuckDB schema initialization and state persistence."""
    db_file = str(tmp_path / "test_quant.db")
    db = DatabaseManager(db_path=db_file)
    try:
        assert not db.is_system_halted()
        db.set_system_halted(True, reason="Unit test halt")
        assert db.is_system_halted()
        assert db.get_state("halt_reason") == "Unit test halt"

        db.upsert_watchlist(
            [
                {
                    "symbol": "BTC/USDT",
                    "rank": 1,
                    "volume_24h_usd": 1_250_000_000.0,
                    "spread_bps": 1.2,
                    "price": 85000.0,
                    "change_24h_pct": 3.4,
                }
            ]
        )
        wl = db.get_watchlist()
        assert len(wl) == 1
        assert wl[0]["symbol"] == "BTC/USDT"
    finally:
        db.close()


@pytest.mark.asyncio
async def test_gemini_primary_success() -> None:
    """Verify Gemini API is called first when configured."""
    def handler(request: httpx.Request) -> httpx.Response:
        if "generativelanguage.googleapis.com" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "candidates": [
                        {"content": {"parts": [{"text": '{"confidence": 0.85, "reason": "Gemini primary"}'}]}}
                    ]
                },
            )
        return httpx.Response(500, json={"error": "should not be reached"})

    cfg = Settings(
        GEMINI_API_KEY="test_gemini_key",
        GEMINI_MODEL="gemini-3.1-flash-lite",
        OPENROUTER_API_KEY_1="sk-or-test",
    )
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        pool = OpenRouterPool(custom_settings=cfg, http_client=client)
        res = await pool.complete_with_fallback("test prompt", "fallback")
        assert res["source"] == "gemini"
        assert res["model"] == "gemini-3.1-flash-lite"
        assert "Gemini primary" in res["content"]

