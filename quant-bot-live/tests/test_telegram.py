"""Unit tests for TelegramNotifier integration."""

import pytest
import httpx
from engine.telegram_bot import TelegramNotifier
from config.settings import Settings

@pytest.mark.asyncio
async def test_telegram_notifier_disabled_without_keys():
    cfg = Settings(TELEGRAM_BOT_TOKEN="", TELEGRAM_CHAT_ID="")
    t = TelegramNotifier(custom_settings=cfg)
    assert t.enabled is False
    res = await t.send_message("Test")
    assert res is False

@pytest.mark.asyncio
async def test_telegram_notifier_send_message_mock():
    seen = []
    def handler(request: httpx.Request):
        seen.append(request)
        return httpx.Response(200, json={"ok": True, "result": {}})

    cfg = Settings(TELEGRAM_BOT_TOKEN="mock_tok", TELEGRAM_CHAT_ID="12345")
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        t = TelegramNotifier(custom_settings=cfg)
        t._client = client
        sent = await t.send_message("Hello from test")
        assert sent is True
        assert len(seen) == 1
        assert "sendMessage" in str(seen[0].url)
