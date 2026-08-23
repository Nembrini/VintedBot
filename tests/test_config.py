"""Tests for vintedbot.config.Settings."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from vintedbot.config import Settings


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate each test from the developer's real env / .env file."""
    for var in (
        "VINTEDBOT_BASE_URL",
        "VINTEDBOT_RATE_LIMIT_PER_MINUTE",
        "VINTEDBOT_REQUEST_TIMEOUT_SECONDS",
        "VINTEDBOT_CONNECT_TIMEOUT_SECONDS",
        "VINTEDBOT_SEARCH_MAX_PAGES",
        "VINTEDBOT_SEARCH_MAX_ITEMS",
        "VINTEDBOT_DB_PATH",
        "VINTEDBOT_LOG_LEVEL",
        "VINTEDBOT_LOG_JSON",
    ):
        monkeypatch.delenv(var, raising=False)


def _settings(**kwargs: object) -> Settings:
    # _env_file=None: ignore any local .env so tests are deterministic.
    return Settings(_env_file=None, **kwargs)  # type: ignore[call-arg]


def test_defaults() -> None:
    s = _settings()
    assert str(s.base_url) == "https://www.vinted.it/"
    assert s.rate_limit_per_minute == 12
    assert s.request_timeout_seconds == 30.0
    assert s.search_max_pages == 5
    assert s.search_max_items == 200
    assert s.log_level == "INFO"
    assert s.log_json is False


def test_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VINTEDBOT_BASE_URL", "https://www.vinted.fr")
    monkeypatch.setenv("VINTEDBOT_RATE_LIMIT_PER_MINUTE", "5")
    monkeypatch.setenv("VINTEDBOT_LOG_LEVEL", "debug")  # lowercase: normalized
    s = _settings()
    assert str(s.base_url) == "https://www.vinted.fr/"
    assert s.rate_limit_per_minute == 5
    assert s.log_level == "DEBUG"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("rate_limit_per_minute", 0),
        ("rate_limit_per_minute", 999),
        ("request_timeout_seconds", -1),
        ("log_level", "VERBOSE"),
        ("base_url", "not-a-url"),
    ],
)
def test_invalid_values_rejected(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        _settings(**{field: value})
