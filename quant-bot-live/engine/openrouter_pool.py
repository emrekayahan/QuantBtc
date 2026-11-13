"""Multi-key OpenRouter rotator (3 keys) with automatic model fallback and instant deterministic fallback."""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Dict, List, Optional

import httpx

from config.settings import Settings, settings

logger = logging.getLogger(__name__)


class OpenRouterPool:
    """Manages round-robin rotation across up to 3 OpenRouter free-tier API keys and fallback models."""

    def __init__(
        self,
        custom_settings: Optional[Settings] = None,
        http_client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self.cfg = custom_settings or settings
        self.gemini_key: str = str(getattr(self.cfg, "GEMINI_API_KEY", "") or "").strip()
        self.gemini_model: str = str(getattr(self.cfg, "GEMINI_MODEL", "gemini-3.1-flash-lite") or "gemini-3.1-flash-lite").strip()
        self.keys: List[str] = list(self.cfg.openrouter_keys)
        self.models: List[str] = list(self.cfg.openrouter_model_list)
        self.timeout_seconds: float = float(self.cfg.OPENROUTER_TIMEOUT_SECONDS)
        self.base_url: str = self.cfg.OPENROUTER_BASE_URL
        self._key_index: int = 0
        self._client: Optional[httpx.AsyncClient] = http_client

        # Telemetry per key index
        self.key_telemetry: Dict[int, Dict[str, Any]] = {
            idx: {
                "masked_key": f"...{k[-6:]}" if len(k) >= 6 else f"key_{idx + 1}",
                "status": "healthy",
                "requests": 0,
                "failures": 0,
                "rate_limits": 0,
                "last_used_ms": 0,
                "last_error": "",
            }
            for idx, k in enumerate(self.keys)
        }

    def get_current_key_index(self) -> int:
        if not self.keys:
            return -1
        return self._key_index % len(self.keys)

    def rotate_key(self) -> int:
        if not self.keys:
            return -1
        self._key_index = (self._key_index + 1) % len(self.keys)
        return self._key_index

    def get_telemetry(self) -> Dict[str, Any]:
        return {
            "gemini": {
                "configured": bool(self.gemini_key and not self.gemini_key.startswith("your_")),
                "model": self.gemini_model,
                "masked_key": f"...{self.gemini_key[-6:]}" if len(self.gemini_key) >= 6 else "",
            },
            "configured_keys": len(self.keys),
            "active_key_index": self.get_current_key_index(),
            "models": self.models,
            "keys": list(self.key_telemetry.values()),
        }

    async def complete_with_fallback(
        self,
        prompt: str,
        deterministic_fallback: str | Callable[[], str],
        system_prompt: str = (
            "You are a quantitative cryptocurrency market analyst. "
            "Provide concise, objective analysis based strictly on the provided numeric telemetry."
        ),
        max_tokens: int = 350,
    ) -> Dict[str, Any]:
        """Attempt completion across keys and models; immediately return deterministic_fallback on failure."""
        fallback_text = (
            deterministic_fallback() if callable(deterministic_fallback) else str(deterministic_fallback)
        )

        own_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=self.timeout_seconds)

        try:
            # Priority Tier 1: Google AI Studio Gemini API (if configured)
            if self.gemini_key and not self.gemini_key.startswith("your_"):
                try:
                    url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.gemini_model}:generateContent?key={self.gemini_key}"
                    combined_prompt = f"{system_prompt}\n\n{prompt}"
                    payload = {
                        "contents": [{"parts": [{"text": combined_prompt}]}],
                        "generationConfig": {
                            "temperature": 0.1,
                            "maxOutputTokens": max_tokens,
                            "responseMimeType": "application/json",
                        },
                    }
                    resp = await client.post(url, json=payload, timeout=min(self.timeout_seconds, 6.0))
                    if resp.status_code == 200:
                        data = resp.json()
                        candidates = data.get("candidates") or []
                        if candidates:
                            parts = candidates[0].get("content", {}).get("parts", [])
                            if parts and parts[0].get("text"):
                                txt = str(parts[0]["text"]).strip()
                                return {
                                    "content": txt,
                                    "source": "gemini",
                                    "key_index": "gemini",
                                    "model": self.gemini_model,
                                }
                    logger.warning("Gemini API HTTP %d, falling back to OpenRouter pool.", resp.status_code)
                except Exception as g_exc:
                    logger.warning("Gemini API call failed (%s); falling back to OpenRouter pool.", g_exc)

            if not self.keys:
                logger.warning("No valid OpenRouter keys configured; returning deterministic fallback.")
                return {
                    "content": fallback_text,
                    "source": "deterministic_fallback",
                    "key_index": None,
                    "model": None,
                }

            for model in self.models:
                for _ in range(len(self.keys)):
                    key_idx = self.get_current_key_index()
                    api_key = self.keys[key_idx]
                    self.rotate_key()

                    telemetry = self.key_telemetry[key_idx]
                    telemetry["requests"] += 1
                    telemetry["last_used_ms"] = int(time.time() * 1000)

                    headers = {
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                        "HTTP-Referer": "http://localhost:8000",
                        "X-Title": "QuantBotLive",
                    }
                    payload = {
                        "model": model,
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": prompt},
                        ],
                        "max_tokens": max_tokens,
                        "temperature": 0.1,
                    }

                    try:
                        resp = await client.post(
                            self.base_url,
                            headers=headers,
                            json=payload,
                            timeout=self.timeout_seconds,
                        )
                        if resp.status_code == 200:
                            data = resp.json()
                            choices = data.get("choices") or []
                            msg_obj = choices[0].get("message", {}) if choices else {}
                            # Strictly read final 'content' field; never fall back to internal 'reasoning' CoT leak
                            raw_text = msg_obj.get("content")
                            if raw_text and str(raw_text).strip():
                                content = str(raw_text).strip()
                                telemetry["status"] = "healthy"
                                telemetry["last_error"] = ""
                                return {
                                    "content": content,
                                    "source": "openrouter",
                                    "key_index": key_idx,
                                    "model": model,
                                }
                            telemetry["failures"] += 1
                            telemetry["status"] = "degraded"
                            telemetry["last_error"] = "Empty choices payload"
                        elif resp.status_code == 429:
                            telemetry["rate_limits"] += 1
                            telemetry["failures"] += 1
                            telemetry["status"] = "rate_limited"
                            telemetry["last_error"] = "HTTP 429 Rate Limit"
                            logger.warning(
                                "OpenRouter HTTP 429 on key #%d (%s), rotating immediately.",
                                key_idx + 1,
                                model,
                            )
                        else:
                            telemetry["failures"] += 1
                            telemetry["status"] = "degraded"
                            telemetry["last_error"] = f"HTTP {resp.status_code}"
                            logger.warning(
                                "OpenRouter HTTP %d on key #%d (%s), rotating.",
                                resp.status_code,
                                key_idx + 1,
                                model,
                            )
                    except (httpx.TimeoutException, httpx.RequestError, Exception) as exc:
                        telemetry["failures"] += 1
                        telemetry["status"] = "degraded"
                        telemetry["last_error"] = type(exc).__name__
                        logger.warning(
                            "OpenRouter exception (%s) on key #%d (%s), rotating.",
                            exc,
                            key_idx + 1,
                            model,
                        )
        finally:
            if own_client:
                await client.aclose()

        logger.warning("All OpenRouter keys and models exhausted; returning deterministic fallback.")
        return {
            "content": fallback_text,
            "source": "deterministic_fallback",
            "key_index": None,
            "model": None,
        }
