"""Application configuration, loaded from environment variables / .env.

All knobs live here — no hardcoded values elsewhere in the codebase.
Every variable is prefixed with ``VINTEDBOT_`` (e.g. ``VINTEDBOT_BASE_URL``).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, HttpUrl, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from vintedbot.paths import default_data_dir

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
    data_dir: Path = Field(
        default_factory=default_data_dir,
        description=(
            "Directory for database, logs and lock file. Defaults to the "
            "per-user data dir, deliberately OUTSIDE cloud-synced folders."
        ),
    )
    db_path: Path = Field(
        default_factory=lambda: default_data_dir() / "vintedbot.db",
        description=(
            "SQLite database file; follows data_dir unless set explicitly. "
            "Parent directory is created on first open."
        ),
    )
    max_run_seconds: float = Field(
        default=600.0,
        gt=0,
        le=86400,
        description="Watchdog: hard deadline for one `run-all` execution.",
    )
    log_max_bytes: int = Field(
        default=5 * 1024 * 1024,
        ge=1024,
        description="Rotate the log file once it reaches this size.",
    )
    log_backup_count: int = Field(
        default=5,
        ge=0,
        le=50,
        description="How many rotated log files to keep.",
    )
    error_notify_cooldown_hours: float = Field(
        default=6.0,
        ge=0,
        le=720,
        description="Minimum delay before re-notifying the SAME failure signature.",
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
    searches_path: Path = Field(
        default=Path("searches.toml"),
        description="TOML file holding the saved searches executed by `run-all`.",
    )
    delay_between_searches_seconds: float = Field(
        default=5.0,
        ge=0,
        le=300,
        description="Pause between two saved searches in a `run-all` execution.",
    )
    pricing_min_sample_size: int = Field(
        default=8,
        ge=1,
        le=1000,
        description="Below this many deduped observations the score is None (unknown).",
    )
    pricing_max_discount: float = Field(
        default=0.60,
        gt=0,
        le=1,
        description="Discount vs median that maps to the top of the score curve.",
    )
    pricing_confidence_k: int = Field(
        default=10,
        ge=0,
        le=1000,
        description="Shrinkage constant: confidence = n / (n + k).",
    )
    pricing_max_age_days: int = Field(
        default=90,
        ge=1,
        le=3650,
        description="Observation window for the market estimate.",
    )
    max_notifications_per_run: int = Field(
        default=10,
        ge=1,
        le=100,
        description=(
            "Anti-flood cap: max Telegram notifications per run. Items beyond "
            "the cap stay unnotified and are drained on later runs."
        ),
    )
    notify_pause_seconds: float = Field(
        default=1.0,
        ge=0,
        le=30,
        description="Pause between two Telegram sends (stay clear of API limits).",
    )
    telegram_bot_token: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("VINTEDBOT_TELEGRAM_BOT_TOKEN", "TELEGRAM_BOT_TOKEN"),
        description=(
            "Telegram bot token (from @BotFather). Optional at type level: the "
            "search CLI works without it; the notifier validates presence at use."
        ),
    )
    telegram_chat_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices("VINTEDBOT_TELEGRAM_CHAT_ID", "TELEGRAM_CHAT_ID"),
        description="Chat that receives the notifications.",
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

    @model_validator(mode="after")
    def _db_path_follows_data_dir(self) -> Settings:
        """Keep the DB inside data_dir unless the user pinned db_path itself."""
        if "db_path" not in self.model_fields_set:
            self.db_path = self.data_dir / "vintedbot.db"
        return self

    @property
    def log_dir(self) -> Path:
        """Directory holding the rotating log files."""
        return self.data_dir / "logs"

    @property
    def lock_path(self) -> Path:
        """Single-instance lock file for `run-all`."""
        return self.data_dir / "vintedbot.lock"

    @property
    def health_path(self) -> Path:
        """Failure-tracking state (JSON, deliberately not in the DB)."""
        return self.data_dir / "health.json"

    def secret_values(self) -> tuple[str, ...]:
        """Strings that must never reach a log line (see log.mask_secrets)."""
        secrets: list[str] = []
        if self.telegram_bot_token is not None:
            secrets.append(self.telegram_bot_token.get_secret_value())
        # Short ids are skipped: masking "42" everywhere would mangle logs.
        if self.telegram_chat_id and len(self.telegram_chat_id) >= 5:
            secrets.append(self.telegram_chat_id)
        return tuple(secrets)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton (cached after first call)."""
    return Settings()
