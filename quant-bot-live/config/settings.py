"""Pydantic BaseSettings with strict defaults and validation."""

from __future__ import annotations

from pathlib import Path
from typing import List
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """Master configuration for the Autonomous Quantitative Trading Engine."""

    model_config = SettingsConfigDict(
        env_file=str(BASE_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        validate_default=True,
    )

    # === REQUIRED OPERATOR CREDENTIALS ===
    BINANCE_API_KEY: str = Field(default="your_binance_spot_api_key_here")
    BINANCE_API_SECRET: str = Field(default="your_binance_spot_api_secret_here")

    # OpenRouter free-tier API keys (1 to 3 keys)
    OPENROUTER_API_KEY_1: str = Field(default="")
    OPENROUTER_API_KEY_2: str = Field(default="")
    OPENROUTER_API_KEY_3: str = Field(default="")

    # Comma-separated fallback chain of free OpenRouter models
    OPENROUTER_MODELS: str = Field(
        default="nvidia/nemotron-3-ultra-550b-a55b:free,liquid/lfm-3-1.7b:free,google/gemma-4-31b-it:free"
    )
    OPENROUTER_BASE_URL: str = Field(default="https://openrouter.ai/api/v1/chat/completions")
    OPENROUTER_TIMEOUT_SECONDS: float = Field(default=8.0, gt=0.0, le=30.0)

    # === TELEGRAM NOTIFICATIONS & REMOTE CONTROL ===
    TELEGRAM_BOT_TOKEN: str = Field(default="")
    TELEGRAM_CHAT_ID: str = Field(default="")

    # === DASHBOARD SECURITY (HTTP BASIC AUTH) ===
    DASHBOARD_USERNAME: str = Field(default="admin")
    DASHBOARD_PASSWORD: str = Field(default="change_me_strong_password")

    # === RISK & CAPITAL GUARDRAILS ===
    TIMEFRAME: str = Field(default="15m")
    MAX_OPEN_POSITIONS: int = Field(default=3, ge=1, le=20)
    MAX_POSITION_USD: float = Field(default=25.0, gt=0.0)
    SPOT_MIN_POSITION_USD: float = Field(default=11.50, gt=0.0)
    FUTURES_MIN_USDT: float = Field(default=3.0, gt=0.0)
    FUTURES_DEFAULT_LEVERAGE: int = Field(default=20, ge=1, le=50)
    FUTURES_MARGIN_USD: float = Field(default=4.50, gt=0.0)
    DAILY_MAX_TRADES: int = Field(default=20, ge=1, le=100)
    DAILY_MAX_LOSS_USD: float = Field(default=5.0, gt=0.0)
    MAX_DRAWDOWN_PCT: float = Field(default=15.0, gt=0.0, le=100.0)
    MIN_24H_VOLUME_USD: float = Field(default=10_000_000.0, gt=0.0)
    MAX_SPREAD_PCT: float = Field(default=0.20, gt=0.0, le=5.0)
    MIN_CONFIDENCE_THRESHOLD: float = Field(default=0.68, ge=0.50, le=0.99)
    MIN_SETUP_QUALITY: int = Field(default=2, ge=1, le=3)
    ROUND_TRIP_FEE_PCT: float = Field(default=0.20, ge=0.0, le=2.0)

    # === STORAGE, LOGS & DASHBOARD ===
    DASHBOARD_HOST: str = Field(default="0.0.0.0")
    DASHBOARD_PORT: int = Field(default=8000, ge=1024, le=65535)
    DB_PATH: str = Field(default=str(BASE_DIR / "data" / "quant_bot.db"))
    LOG_DIR: str = Field(default=str(BASE_DIR / "logs"))
    TOP_WATCHLIST_SIZE: int = Field(default=20, ge=5, le=50)

    @field_validator("TIMEFRAME")
    @classmethod
    def validate_timeframe(cls, v: str) -> str:
        allowed = {"1m", "3m", "5m", "15m", "30m", "1h", "4h"}
        if v not in allowed:
            raise ValueError(f"TIMEFRAME must be one of {sorted(allowed)}, got {v!r}")
        return v

    @property
    def openrouter_keys(self) -> List[str]:
        """Return list of configured non-placeholder OpenRouter API keys."""
        raw_keys = [
            self.OPENROUTER_API_KEY_1,
            self.OPENROUTER_API_KEY_2,
            self.OPENROUTER_API_KEY_3,
        ]
        placeholders = {
            "",
            "sk-or-v1-first_free_key",
            "sk-or-v1-second_free_key",
            "sk-or-v1-third_free_key",
        }
        return [k.strip() for k in raw_keys if k and k.strip() not in placeholders]

    @property
    def openrouter_model_list(self) -> List[str]:
        """Return parsed list of fallback OpenRouter model identifiers."""
        models = [m.strip() for m in self.OPENROUTER_MODELS.split(",") if m.strip()]
        return models or ["deepseek/deepseek-r1:free"]

    @property
    def has_valid_binance_keys(self) -> bool:
        """Check whether operator has replaced placeholder Binance credentials."""
        placeholders = {
            "",
            "your_binance_spot_api_key_here",
            "your_binance_spot_api_secret_here",
        }
        return (
            bool(self.BINANCE_API_KEY.strip())
            and bool(self.BINANCE_API_SECRET.strip())
            and self.BINANCE_API_KEY.strip() not in placeholders
            and self.BINANCE_API_SECRET.strip() not in placeholders
        )


def get_settings() -> Settings:
    """Create a fresh Settings instance."""
    return Settings()


settings = get_settings()
