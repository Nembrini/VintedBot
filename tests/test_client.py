"""Tests for VintedClient retry/rate-limit behavior. The transport is faked:
no real network, no real sleeping (asyncio.sleep is monkeypatched)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from vintedbot.client import (
    VintedClient,
    VintedError,
    VintedRateLimitError,
)
from vintedbot.config import Settings
from vintedbot.models import SearchFilters

FILTERS = SearchFilters(category_ids=(2536,))


class FakeResponse:
    def __init__(
        self,
        status_code: int,
        json_data: Any = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self._json_data = json_data
        self.headers = headers or {}

    def json(self) -> Any:
        if self._json_data is None:
            raise ValueError("no JSON")
        return self._json_data


class FakeSession:
    """Serves a scripted list of responses and records every GET."""

    def __init__(self, responses: list[FakeResponse]) -> None:
        self._responses = responses
        self.calls: list[str] = []
        self.cookies = {"access_token_web": "fake"}

    async def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append(url)
        return self._responses.pop(0)


@pytest.fixture()
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record every asyncio.sleep delay instead of actually sleeping."""
    recorded: list[float] = []

    async def fake_sleep(delay: float) -> None:
        recorded.append(delay)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    return recorded


def make_client(fake: FakeSession) -> VintedClient:
    settings = Settings(_env_file=None, rate_limit_per_minute=60)
    client = VintedClient(settings)
    client._session = fake  # type: ignore[assignment]  # inject fake transport
    client._bootstrapped = True
    return client


# --------------------------------------------------- (f) retry / rate limit


async def test_429_respects_retry_after_then_succeeds(sleeps: list[float]) -> None:
    fake = FakeSession(
        [
            FakeResponse(429, headers={"Retry-After": "2"}),
            FakeResponse(200, json_data={"items": []}),
        ]
    )
    client = make_client(fake)

    items = await client.search(FILTERS)

    assert items == []
    assert len(fake.calls) == 2
    assert sleeps == [2.0]  # ha atteso ESATTAMENTE quanto chiesto dal server


async def test_429_exhausted_raises_rate_limit_error(sleeps: list[float]) -> None:
    fake = FakeSession([FakeResponse(429, headers={"Retry-After": "1"})] * 3)
    client = make_client(fake)

    with pytest.raises(VintedRateLimitError) as excinfo:
        await client.search(FILTERS)
    assert excinfo.value.retry_after == 1.0
    assert len(fake.calls) == 3  # max_retries di default


async def test_application_4xx_is_never_retried(sleeps: list[float]) -> None:
    fake = FakeSession([FakeResponse(404)])
    client = make_client(fake)

    with pytest.raises(VintedError, match="404"):
        await client.search(FILTERS)
    assert len(fake.calls) == 1  # una sola richiesta, zero retry
    assert sleeps == []


async def test_5xx_is_retried_then_succeeds(sleeps: list[float]) -> None:
    fake = FakeSession(
        [FakeResponse(500), FakeResponse(200, json_data={"items": []})]
    )
    client = make_client(fake)

    items = await client.search(FILTERS)

    assert items == []
    assert len(fake.calls) == 2
    assert len(sleeps) == 1  # un backoff tra i due tentativi


async def test_401_triggers_one_session_refresh(sleeps: list[float]) -> None:
    fake = FakeSession(
        [
            FakeResponse(401),  # token scaduto
            FakeResponse(200),  # re-bootstrap homepage
            FakeResponse(200, json_data={"items": []}),  # retry API ok
        ]
    )
    client = make_client(fake)

    items = await client.search(FILTERS)

    assert items == []
    assert len(fake.calls) == 3
    assert fake.calls[1].endswith("/")  # la seconda chiamata è il bootstrap
