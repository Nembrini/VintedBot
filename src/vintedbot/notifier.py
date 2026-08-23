"""Minimal Telegram notifier over the raw Bot HTTP API.

Deliberately NOT python-telegram-bot: sending a message is one HTTP
endpoint and we want zero heavy dependencies. The HTTP stack is
``curl_cffi``'s ``AsyncSession`` — the same library as the Vinted client
(one stack for the whole project; no impersonation needed here) — with
the same retry-with-backoff policy for transient failures.

Secrecy rules: the bot token lives in the request URL, so the URL is
NEVER logged and any error text that could echo it is masked. Logs never
contain token, chat_id, or message content.
"""

from __future__ import annotations

import asyncio
import random
import re
import time
from typing import TYPE_CHECKING, Any, Self

import structlog
from curl_cffi import requests as curl_requests
from curl_cffi.requests.exceptions import RequestException

from vintedbot.config import get_settings
from vintedbot.formatting import format_item_message

if TYPE_CHECKING:
    from types import TracebackType

    from vintedbot.config import Settings
    from vintedbot.models import Item
    from vintedbot.pricing import PriceEstimate

logger = structlog.get_logger(__name__)

_API_BASE = "https://api.telegram.org"

#: Frammenti (lowercase) delle description Telegram che indicano un problema
#: con la FOTO (URL scaduto/irraggiungibile/contenuto non valido): solo
#: questi errori attivano il fallback testuale di ``send_item``.
_PHOTO_ERROR_MARKERS = (
    "wrong file identifier",
    "http url specified",
    "failed to get http url content",
    "wrong type of the web page content",
    "image_process_failed",
    "group send failed",
    "webpage_curl_failed",       # Telegram non riesce a scaricare l'URL (CDN Vinted)
    "failed to send message #",  # sendMediaGroup: fallito il download del media N
    "photo",
)

#: Telegram cap: an album (sendMediaGroup) holds at most 10 media.
_ALBUM_MAX_PHOTOS = 10

#: Quante foto rotte siamo disposti a scartare (un retry dell'album l'una)
#: prima di degradare a foto singola.
_ALBUM_MAX_DROPS = 3

#: "Bad Request: failed to send message #7 with the error message …"
_FAILED_MEDIA_RE = re.compile(r"failed to send message #(\d+)")


class TelegramError(Exception):
    """A Telegram API call failed definitively.

    Attributes:
        status_code: HTTP status of the failed call, when applicable.
        description: the (token-masked) API error description, when present.
    """

    def __init__(
        self, message: str, *, status_code: int | None = None, description: str = ""
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.description = description


class TelegramConfigError(TelegramError):
    """Telegram credentials are missing from the configuration."""


class TelegramNotifier:
    """Sends text messages to one chat via the Telegram Bot API.

    Usage::

        async with TelegramNotifier() as notifier:
            await notifier.send_text("hello")

    Credentials are validated HERE, at construction — not in the config
    model — so the rest of the CLI keeps working without them.

    Raises:
        TelegramConfigError: if token or chat id are not configured.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        disable_web_page_preview: bool = True,
        max_retries: int = 3,
        backoff_base_seconds: float = 1.0,
    ) -> None:
        settings = settings or get_settings()
        if settings.telegram_bot_token is None or not settings.telegram_chat_id:
            raise TelegramConfigError(
                "credenziali Telegram mancanti: imposta TELEGRAM_BOT_TOKEN e "
                "TELEGRAM_CHAT_ID (o le varianti VINTEDBOT_*) nel file .env"
            )
        self._token = settings.telegram_bot_token.get_secret_value()
        self._chat_id = settings.telegram_chat_id
        self._disable_preview = disable_web_page_preview
        self._max_retries = max_retries
        self._backoff_base = backoff_base_seconds
        self._timeout = (settings.connect_timeout_seconds, settings.request_timeout_seconds)
        self._session: curl_requests.AsyncSession | None = None

    async def __aenter__(self) -> Self:
        self._session = curl_requests.AsyncSession(timeout=self._timeout)
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

    # ------------------------------------------------------------------ API

    async def send_text(self, text: str) -> None:
        """Send a plain-text message to the configured chat.

        Retry policy:
        - network errors / 5xx: exponential backoff + jitter, up to
          ``max_retries`` attempts (same policy as the Vinted client);
        - 429: honor ``parameters.retry_after`` from the body, retry ONCE,
          then give up;
        - definitive errors (400/401/403…): immediate :class:`TelegramError`
          with a hint at the likely cause. No retry.
        """
        await self._call_api(
            "sendMessage",
            {
                "chat_id": self._chat_id,
                "text": text,
                "disable_web_page_preview": self._disable_preview,
            },
        )
        logger.info("telegram_message_sent")

    async def send_item(self, item: Item, estimate: PriceEstimate | None = None) -> None:
        """Send one item with ALL its photos + HTML caption; degrade gracefully.

        - 2+ photos: ``sendMediaGroup`` (Telegram album, max 10; the caption
          rides on the first photo). Telegram downloads the URLs itself —
          we never fetch the images.
        - 1 photo: ``sendPhoto``.
        - 0 photos: straight to ``sendMessage``.
        - Photo-related 400s (expired/unreachable URL, Telegram unable to
          download a media — ``WEBPAGE_CURL_FAILED``) trigger a degradation
          chain: album → single main photo → text-only. The notification
          must arrive anyway. Any other error (401 token, chat not found, …)
          propagates exactly like in ``send_text`` — no fallback.
        """
        caption = format_item_message(item, estimate)
        photos = item.photo_urls[:_ALBUM_MAX_PHOTOS]
        if not photos and item.photo_url:
            photos = (item.photo_url,)  # righe pre-v3: solo la principale

        if len(photos) > 1:
            photos = await self._send_album(item.id, list(photos), caption)
            if not photos:
                return  # album partito (eventualmente senza le foto rotte)

        if photos:
            try:
                await self._call_api(
                    "sendPhoto",
                    {
                        "chat_id": self._chat_id,
                        "photo": photos[0],
                        "caption": caption,
                        "parse_mode": "HTML",
                    },
                )
                logger.info("telegram_item_sent", item_id=item.id, via="photo", photos=1)
                return
            except TelegramError as exc:
                if not _is_photo_error(exc):
                    raise
                logger.warning(
                    "telegram_photo_failed_falling_back",
                    item_id=item.id,
                    description=exc.description,
                )

        await self._call_api("sendMessage", self._html_text_payload(caption))
        logger.info("telegram_item_sent", item_id=item.id, via="text", photos=0)

    # ------------------------------------------------------------- internals

    async def _send_album(
        self, item_id: int, urls: list[str], caption: str
    ) -> tuple[str, ...]:
        """Try sendMediaGroup, dropping the specific broken photo and retrying.

        Telegram's error names the failing media ("failed to send message
        #N"): we remove just that one and retry, up to
        :data:`_ALBUM_MAX_DROPS` times. Returns ``()`` when an album was
        sent; otherwise the remaining photos for the caller's next
        degradation step (single photo → text). Non-photo errors propagate.
        """
        drops = 0
        while len(urls) > 1 and drops <= _ALBUM_MAX_DROPS:
            media: list[dict[str, Any]] = [{"type": "photo", "media": url} for url in urls]
            media[0]["caption"] = caption
            media[0]["parse_mode"] = "HTML"
            try:
                await self._call_api(
                    "sendMediaGroup", {"chat_id": self._chat_id, "media": media}
                )
                logger.info(
                    "telegram_item_sent",
                    item_id=item_id,
                    via="album",
                    photos=len(urls),
                    dropped=drops,
                )
                return ()
            except TelegramError as exc:
                if not _is_photo_error(exc):
                    raise
                match = _FAILED_MEDIA_RE.search(exc.description)
                index = int(match.group(1)) if match else None
                if index is None or not (1 <= index <= len(urls)):
                    break  # foto rotta non identificabile: degrada
                dropped_url = urls.pop(index - 1)
                drops += 1
                logger.warning(
                    "telegram_album_photo_dropped",
                    item_id=item_id,
                    media_index=index,
                    remaining=len(urls),
                    dropped_url=dropped_url,
                )

        logger.warning("telegram_album_failed_degrading", item_id=item_id)
        return tuple(urls[:1])

    def _html_text_payload(self, text: str) -> dict[str, Any]:
        """Payload for an HTML-formatted sendMessage (item captions)."""
        return {
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": self._disable_preview,
        }

    def _mask(self, text: str) -> str:
        """Strip the bot token from any text that might get logged/raised."""
        return text.replace(self._token, "***TOKEN***")

    async def _call_api(self, method: str, payload: dict[str, Any]) -> None:
        if self._session is None:
            raise TelegramError("TelegramNotifier must be used as an async context manager")
        # L'URL contiene il token: non va MAI loggato non mascherato.
        url = f"{_API_BASE}/bot{self._token}/{method}"
        log = logger.bind(method=method)
        retried_429 = False

        attempt = 1
        while True:
            start = time.perf_counter()
            try:
                resp = await self._session.post(url, json=payload)
            except RequestException as exc:
                log.warning(
                    "telegram_network_error", attempt=attempt, error=exc.__class__.__name__
                )
                if attempt >= self._max_retries:
                    raise TelegramError(
                        f"errore di rete verso Telegram dopo {attempt} tentativi"
                    ) from exc
                await asyncio.sleep(self._backoff_delay(attempt))
                attempt += 1
                continue

            duration_ms = round((time.perf_counter() - start) * 1000)
            log.debug("telegram_request_done", status=resp.status_code,
                      duration_ms=duration_ms, attempt=attempt)

            body: dict[str, Any] = {}
            try:
                parsed = resp.json()  # type: ignore[no-untyped-call]
                if isinstance(parsed, dict):
                    body = parsed
            except ValueError:
                pass
            description = self._mask(str(body.get("description", "")))

            if resp.status_code == 200 and body.get("ok") is True:
                return
            if resp.status_code == 200:
                # 200 ma ok:false — anomalo, trattalo come definitivo.
                log.error("telegram_api_error", description=description)
                raise TelegramError(
                    f"Telegram ha rifiutato il messaggio: {description}",
                    status_code=200,
                    description=description,
                )

            if resp.status_code == 429:
                retry_after = float(
                    body.get("parameters", {}).get("retry_after", self._backoff_base)
                )
                if retried_429:
                    log.error("telegram_rate_limited_giving_up", retry_after=retry_after)
                    raise TelegramError(
                        f"rate limit Telegram persistente (retry_after={retry_after}s)"
                    )
                log.warning("telegram_rate_limited", retry_after=retry_after)
                retried_429 = True
                await asyncio.sleep(retry_after)
                continue  # un solo retry per il 429

            if 500 <= resp.status_code < 600:
                log.warning("telegram_server_error", status=resp.status_code, attempt=attempt)
                if attempt >= self._max_retries:
                    raise TelegramError(
                        f"Telegram HTTP {resp.status_code} dopo {attempt} tentativi"
                    )
                await asyncio.sleep(self._backoff_delay(attempt))
                attempt += 1
                continue

            # Errori definitivi: mai ritentati, messaggio con causa probabile.
            hint = {
                400: "chat_id errato, oppure non hai mai avviato il bot con /start",
                401: "bot token invalido o revocato",
                403: "il bot è stato bloccato dall'utente o rimosso dalla chat",
            }.get(resp.status_code, "errore non ritentabile")
            log.error("telegram_fatal_error", status=resp.status_code, description=description)
            raise TelegramError(
                f"Telegram HTTP {resp.status_code} ({hint}). "
                f"Descrizione API: {description or 'n/d'}",
                status_code=resp.status_code,
                description=description,
            )

    def _backoff_delay(self, attempt: int) -> float:
        """Exponential backoff with uniform jitter: base·2^(n-1) + U(0, base)."""
        return self._backoff_base * 2.0 ** (attempt - 1) + random.uniform(0, self._backoff_base)


def is_fatal_config_error(exc: TelegramError) -> bool:
    """True for errors that make EVERY further send pointless.

    401 (token invalido), 403 (bot bloccato/rimosso) e 400 "chat not found"
    sono problemi di configurazione: ritentare gli altri item della coda è
    inutile. Tutto il resto (foto rotta, rate limit persistente, rete) è
    specifico del singolo invio.
    """
    if exc.status_code in (401, 403):
        return True
    return exc.status_code == 400 and "chat not found" in exc.description.lower()


def _is_photo_error(exc: TelegramError) -> bool:
    """True when a 400 description points at the photo, not at us."""
    if exc.status_code != 400:
        return False
    description = exc.description.lower()
    return any(marker in description for marker in _PHOTO_ERROR_MARKERS)
