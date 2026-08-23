"""Async HTTP client for Vinted's internal catalog API.

Built on ``curl_cffi``'s ``AsyncSession`` — deliberately NOT httpx: as
documented in ``docs/api_notes.md``, DataDome TLS fingerprinting blocks
plain httpx/requests with 403 before session cookies are ever issued.
``curl_cffi`` impersonates a real Chrome TLS/HTTP2 fingerprint and is the
only verified way in for a lightweight client.

Responsibilities:
- anonymous session bootstrap (homepage GET → cookies) and automatic
  refresh on 401;
- client-side rate limiting (async token bucket, config-driven);
- retry with exponential backoff + jitter on transient failures only;
- domain exceptions instead of leaking transport errors;
- structured request logging (never cookies/tokens).
"""

from __future__ import annotations

import asyncio
import random
import time
from typing import TYPE_CHECKING, Any, Self

import structlog
from curl_cffi import requests as curl_requests
from curl_cffi.requests.exceptions import RequestException

from vintedbot.config import get_settings
from vintedbot.models import parse_items

if TYPE_CHECKING:
    from types import TracebackType

    from vintedbot.config import Settings
    from vintedbot.models import Item, SearchFilters

logger = structlog.get_logger(__name__)

_CATALOG_PATH = "/api/v2/catalog/items"
_API_HEADERS = {"Accept": "application/json"}

#: Session-wide browser-like headers; ``impersonate`` already injects the
#: matching User-Agent & co., we only pin the locale.
DEFAULT_HEADERS = {
    "Accept-Language": "it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7",
}


class VintedError(Exception):
    """Base class for all VintedBot client errors."""


class VintedAuthError(VintedError):
    """The anonymous session could not be established or refreshed."""


class VintedBlockedError(VintedAuthError):
    """HTTP 403 — DataDome block. Back off for a long time; do not insist."""


class VintedRateLimitError(VintedError):
    """HTTP 429 persisted across retries.

    Attributes:
        retry_after: seconds suggested by the server, if it sent Retry-After.
    """

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class VintedParsingError(VintedError):
    """The response body was not the JSON shape we expected."""


class _TokenBucket:
    """Async token bucket enforcing an average of ``per_minute`` requests/min.

    The burst capacity is deliberately small (max 3): we want to stay
    gentle, not fire a full minute's budget at once.
    """

    def __init__(self, per_minute: int) -> None:
        self._rate = per_minute / 60.0
        self._capacity = float(min(per_minute, 3))
        self._tokens = self._capacity
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Wait until a request slot is available, then consume it."""
        async with self._lock:
            while True:
                now = time.monotonic()
                refill = (now - self._updated) * self._rate
                self._tokens = min(self._capacity, self._tokens + refill)
                self._updated = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                await asyncio.sleep((1.0 - self._tokens) / self._rate)


class VintedClient:
    """Async client for the Vinted catalog search endpoint.

    Usage::

        async with VintedClient() as client:
            items = await client.search(filters, page=1)

    The client bootstraps an anonymous session lazily on the first request
    and refreshes it automatically once if the server answers 401.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        headers: dict[str, str] | None = None,
        max_retries: int = 3,
        backoff_base_seconds: float = 1.0,
        impersonate: str = "chrome",
    ) -> None:
        """
        Args:
            settings: app settings; defaults to the process-wide singleton.
            headers: extra/override session headers (merged over defaults).
            max_retries: attempts for transient failures (timeouts, 5xx, 429).
            backoff_base_seconds: base of the exponential backoff.
            impersonate: curl_cffi browser fingerprint profile.
        """
        self._settings = settings or get_settings()
        self._headers = {**DEFAULT_HEADERS, **(headers or {})}
        self._max_retries = max_retries
        self._backoff_base = backoff_base_seconds
        self._impersonate = impersonate
        self._base_url = str(self._settings.base_url).rstrip("/")
        self._limiter = _TokenBucket(self._settings.rate_limit_per_minute)
        self._session: curl_requests.AsyncSession | None = None
        self._bootstrapped = False

    async def __aenter__(self) -> Self:
        self._session = curl_requests.AsyncSession(
            impersonate=self._impersonate,
            headers=self._headers,
            timeout=(
                self._settings.connect_timeout_seconds,
                self._settings.request_timeout_seconds,
            ),
        )
        await self._session.__aenter__()  # type: ignore[no-untyped-call]
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._session is not None:
            await self._session.__aexit__(exc_type, exc, tb)
            self._session = None
            self._bootstrapped = False

    # ------------------------------------------------------------------ API

    async def search(
        self, filters: SearchFilters, page: int = 1, *, per_page: int = 96
    ) -> list[Item]:
        """Run one catalog search and return the parsed items of ``page``.

        Malformed items in the response are logged and skipped (see
        :func:`vintedbot.models.parse_items`); the page never fails as a whole
        for a single bad entry.

        Raises:
            VintedAuthError / VintedBlockedError / VintedRateLimitError /
            VintedParsingError / VintedError on the corresponding failures.
        """
        params: dict[str, str] = {
            "page": str(page),
            "per_page": str(per_page),
            **filters.to_query_params(),
        }
        data = await self._request_json(_CATALOG_PATH, params)
        raw_items = data.get("items") if isinstance(data, dict) else None
        if not isinstance(raw_items, list):
            raise VintedParsingError("response JSON has no 'items' list")
        items = parse_items(raw_items)
        logger.debug(
            "search_page_parsed",
            page=page,
            returned=len(items),
            discarded=len(raw_items) - len(items),
        )
        return items

    # ------------------------------------------------------------- internals

    def _require_session(self) -> curl_requests.AsyncSession:
        if self._session is None:
            raise VintedError("VintedClient must be used as an async context manager")
        return self._session

    async def _bootstrap(self) -> None:
        """GET the homepage to obtain anonymous session cookies (api_notes §2)."""
        session = self._require_session()
        start = time.perf_counter()
        try:
            resp = await session.get(self._base_url + "/")
        except RequestException as exc:
            raise VintedAuthError(f"session bootstrap failed: {exc.__class__.__name__}") from exc
        duration_ms = round((time.perf_counter() - start) * 1000)
        logger.debug("bootstrap_done", status=resp.status_code, duration_ms=duration_ms)
        if resp.status_code == 403:
            raise VintedBlockedError("bootstrap got 403 — likely DataDome block, back off")
        if resp.status_code != 200:
            raise VintedAuthError(f"session bootstrap failed with HTTP {resp.status_code}")
        if "access_token_web" not in session.cookies:
            # Not fatal (cookie names may change) but worth surfacing.
            logger.warning("bootstrap_token_cookie_missing")
        self._bootstrapped = True

    async def _request_json(self, path: str, params: dict[str, str]) -> Any:
        """GET an API path with rate limiting, session refresh and retries."""
        session = self._require_session()
        if not self._bootstrapped:
            await self._bootstrap()

        url = self._base_url + path
        log = logger.bind(endpoint=path, page=params.get("page"))
        session_refreshed = False

        for attempt in range(1, self._max_retries + 1):
            await self._limiter.acquire()
            start = time.perf_counter()
            try:
                resp = await session.get(url, params=params, headers=_API_HEADERS)
            except RequestException as exc:
                # Timeouts / connection errors are transient: retry with backoff.
                log.warning(
                    "request_network_error",
                    attempt=attempt,
                    error=exc.__class__.__name__,
                )
                if attempt == self._max_retries:
                    raise VintedError(f"network failure after {attempt} attempts") from exc
                await self._sleep_backoff(attempt)
                continue

            duration_ms = round((time.perf_counter() - start) * 1000)
            log.debug("request_done", status=resp.status_code, duration_ms=duration_ms,
                      attempt=attempt)

            if resp.status_code == 200:
                try:
                    return resp.json()  # type: ignore[no-untyped-call]
                except ValueError as exc:
                    raise VintedParsingError("response body is not valid JSON") from exc

            if resp.status_code == 401 and not session_refreshed:
                # Expired anonymous token: re-bootstrap once and retry.
                log.info("session_expired_refreshing")
                session_refreshed = True
                self._bootstrapped = False
                await self._bootstrap()
                continue

            if resp.status_code == 403:
                raise VintedBlockedError("HTTP 403 — likely DataDome block, back off")

            if resp.status_code == 429:
                retry_after = _parse_retry_after(resp.headers.get("Retry-After"))
                if attempt == self._max_retries:
                    raise VintedRateLimitError(
                        "HTTP 429 persisted across retries", retry_after=retry_after
                    )
                log.warning("rate_limited", retry_after=retry_after, attempt=attempt)
                await asyncio.sleep(retry_after if retry_after is not None else
                                    self._backoff_delay(attempt))
                continue

            if 500 <= resp.status_code < 600:
                if attempt == self._max_retries:
                    raise VintedError(f"HTTP {resp.status_code} after {attempt} attempts")
                await self._sleep_backoff(attempt)
                continue

            # Any other 4xx is an application error: never retried.
            if resp.status_code == 401:
                raise VintedAuthError("HTTP 401 persisted after session refresh")
            raise VintedError(f"unexpected HTTP {resp.status_code}")

        raise VintedError("retry loop exhausted")  # pragma: no cover — defensive

    def _backoff_delay(self, attempt: int) -> float:
        """Exponential backoff with uniform jitter: base·2^(n-1) + U(0, base)."""
        return self._backoff_base * 2.0 ** (attempt - 1) + random.uniform(0, self._backoff_base)

    async def _sleep_backoff(self, attempt: int) -> None:
        await asyncio.sleep(self._backoff_delay(attempt))


def _parse_retry_after(value: str | None) -> float | None:
    """Parse a Retry-After header (seconds form only; HTTP-date is ignored)."""
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None
