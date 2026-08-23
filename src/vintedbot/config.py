"""Application configuration, loaded from environment variables / .env.

All knobs live here — no hardcoded values elsewhere in the codebase.
Every variable is prefixed with ``VINTEDBOT_`` (e.g. ``VINTEDBOT_BASE_URL``).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, HttpUrl, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class Settings(BaseSettings):
    """Runtime settings for VintedBot."""

    model_config = SettingsConfigDict(
        env_prefix="VINTEDBOT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    base_url: HttpUrl = Field(
        default=HttpUrl("https://www.vinted.it"),
        description="Vinted national domain to target; session cookies are per-domain.",
    )
    rate_limit_per_minute: int = Field(
        default=12,
        ge=1,
        le=60,
        description="Upper bound on outgoing HTTP requests per minute (be gentle).",
    )
    request_timeout_seconds: float = Field(
        default=30.0,
        gt=0,
        le=120,
        description="Read timeout for HTTP calls (seconds).",
    )
    connect_timeout_seconds: float = Field(
        default=10.0,
        gt=0,
        le=60,
        description="Connect timeout for HTTP calls (seconds).",
    )
    db_path: Path = Field(
        default=Path("data/vintedbot.db"),
        description="SQLite database file; parent directory is created on first open.",
    )
    search_max_pages: int = Field(
        default=5,
        ge=1,
        le=50,
        description="Default page cap for a paginated search (be gentle).",
    )
    search_max_items: int = Field(
        default=200,
        ge=1,
        le=2000,
        description="Default item cap for a paginated search.",
    )
    log_level: LogLevel = Field(
        default="INFO",
        description="Minimum log level.",
    )
    log_json: bool = Field(
        default=False,
        description="Emit JSON log lines (for production) instead of pretty console output.",
    )

    @field_validator("log_level", mode="before")
    @classmethod
    def _uppercase_level(cls, v: object) -> object:
        return v.upper() if isinstance(v, str) else v


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton (cached after first call)."""
    return Settings()
