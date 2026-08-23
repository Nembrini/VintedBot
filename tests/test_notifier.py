"""Tests for TelegramNotifier: transport faked, sleeps recorded — zero network."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from vintedbot.config import Settings
from vintedbot.formatting import format_item_message
from vintedbot.models import Item
from vintedbot.notifier import TelegramConfigError, TelegramError, TelegramNotifier

TOKEN = "123456789:AAfaketokenfaketokenfaketoken"
CHAT_ID = "424242"


def make_settings(**overrides: Any) -> Settings:
    return Settings(  # type: ignore[call-arg]  # _env_file è un kwarg di pydantic-settings
        _env_file=None,
        telegram_bot_token=TOKEN,
        telegram_chat_id=CHAT_ID,
        **overrides,
    )


class FakeResponse:
    def __init__(self, status_code: int, json_data: Any = None) -> None:
        self.status_code = status_code
        self._json_data = json_data

    def json(self) -> Any:
        if self._json_data is None:
            raise ValueError("no JSON")
        return self._json_data


class FakeSession:
    """Records every POST (url + json payload) and serves scripted responses."""

    def __init__(self, responses: list[FakeResponse]) -> None:
        self._responses = responses
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def post(self, url: str, json: dict[str, Any] | None = None, **kw: Any) -> FakeResponse:
        self.calls.append((url, json or {}))
        return self._responses.pop(0)


@pytest.fixture()
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    recorded: list[float] = []

    async def fake_sleep(delay: float) -> None:
        recorded.append(delay)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    return recorded


def make_notifier(fake: FakeSession) -> TelegramNotifier:
    notifier = TelegramNotifier(make_settings())
    notifier._session = fake  # type: ignore[assignment]  # inject fake transport
    return notifier


OK = FakeResponse(200, {"ok": True, "result": {}})


# ------------------------------------------------------------ (a) invio ok


async def test_send_text_posts_chat_id_and_text() -> None:
    fake = FakeSession([FakeResponse(200, {"ok": True, "result": {}})])
    notifier = make_notifier(fake)

    await notifier.send_text("ciao mondo")

    assert len(fake.calls) == 1
    url, payload = fake.calls[0]
    assert url == f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    assert payload["chat_id"] == CHAT_ID
    assert payload["text"] == "ciao mondo"
    assert payload["disable_web_page_preview"] is True


# ------------------------------------------------------ (b) ok:false → errore


async def test_ok_false_raises_with_description() -> None:
    fake = FakeSession(
        [FakeResponse(200, {"ok": False, "description": "Bad Request: text is empty"})]
    )
    notifier = make_notifier(fake)

    with pytest.raises(TelegramError, match="text is empty"):
        await notifier.send_text("")


# ------------------------------------------- (c) 429: attende e ritenta una volta


async def test_429_waits_retry_after_then_succeeds(sleeps: list[float]) -> None:
    fake = FakeSession(
        [
            FakeResponse(429, {"ok": False, "parameters": {"retry_after": 7}}),
            FakeResponse(200, {"ok": True, "result": {}}),
        ]
    )
    notifier = make_notifier(fake)

    await notifier.send_text("ciao")

    assert len(fake.calls) == 2
    assert sleeps == [7.0]  # ha atteso ESATTAMENTE retry_after


async def test_429_twice_gives_up(sleeps: list[float]) -> None:
    fake = FakeSession(
        [
            FakeResponse(429, {"ok": False, "parameters": {"retry_after": 1}}),
            FakeResponse(429, {"ok": False, "parameters": {"retry_after": 5}}),
        ]
    )
    notifier = make_notifier(fake)

    with pytest.raises(TelegramError, match="rate limit"):
        await notifier.send_text("ciao")
    assert len(fake.calls) == 2  # UN solo retry, poi resa


# ----------------------------------------------- (d) 401: immediato, zero retry


async def test_401_fails_immediately_without_retry(sleeps: list[float]) -> None:
    fake = FakeSession([FakeResponse(401, {"ok": False, "description": "Unauthorized"})])
    notifier = make_notifier(fake)

    with pytest.raises(TelegramError, match="token invalido"):
        await notifier.send_text("ciao")

    assert len(fake.calls) == 1  # nessun retry
    assert sleeps == []


async def test_error_message_never_leaks_token() -> None:
    fake = FakeSession(
        [FakeResponse(400, {"ok": False, "description": f"bad url /bot{TOKEN}/x"})]
    )
    notifier = make_notifier(fake)

    with pytest.raises(TelegramError) as excinfo:
        await notifier.send_text("ciao")
    assert TOKEN not in str(excinfo.value)
    assert "***TOKEN***" in str(excinfo.value)


# ------------------------------ (e) credenziali assenti: search non ne risente


def test_settings_without_telegram_are_valid(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
        "VINTEDBOT_TELEGRAM_BOT_TOKEN",
        "VINTEDBOT_TELEGRAM_CHAT_ID",
    ):
        monkeypatch.delenv(var, raising=False)

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    # La config è valida senza credenziali (il flusso search — vedi
    # test_cli_flow, che usa proprio Settings senza Telegram — funziona).
    assert settings.telegram_bot_token is None
    assert settings.telegram_chat_id is None

    # Solo l'uso effettivo del notifier le pretende:
    with pytest.raises(TelegramConfigError, match="mancanti"):
        TelegramNotifier(settings)


def test_token_never_in_settings_repr() -> None:
    settings = make_settings()
    assert TOKEN not in repr(settings)  # SecretStr maschera il valore


# ================================================== send_item (step 3.2)


def make_item(*, with_photo: bool = True, n_photos: int = 1) -> Item:
    raw: dict[str, Any] = {
        "id": 99,
        "title": "Giubbotto",
        "price": {"amount": "12.50", "currency_code": "EUR"},
        "brand_title": "Nike",
        "size_title": "M",
        "status": "Ottime",
        "url": "https://www.vinted.it/items/99-giubbotto",
        "user": {"id": 2, "login": "seller"},
    }
    if with_photo:
        raw["photo"] = {"url": "https://images1.vinted.net/photo1.jpeg"}
        raw["photos"] = [
            {"url": f"https://images1.vinted.net/photo{i}.jpeg"} for i in range(1, n_photos + 1)
        ]
    return Item.model_validate(raw)


# --------------------------------------------------- (f) foto + caption


async def test_send_item_with_photo_calls_send_photo() -> None:
    fake = FakeSession([FakeResponse(200, {"ok": True, "result": {}})])
    notifier = make_notifier(fake)
    item = make_item()

    await notifier.send_item(item)

    assert len(fake.calls) == 1
    url, payload = fake.calls[0]
    assert url.endswith("/sendPhoto")
    assert payload["photo"] == "https://images1.vinted.net/photo1.jpeg"
    assert payload["caption"] == format_item_message(item)
    assert payload["parse_mode"] == "HTML"
    assert payload["chat_id"] == CHAT_ID


# ------------------------------------- (g) 400 foto → fallback testuale


async def test_photo_error_falls_back_to_send_message() -> None:
    fake = FakeSession(
        [
            FakeResponse(
                400,
                {"ok": False,
                 "description": "Bad Request: wrong file identifier/HTTP URL specified"},
            ),
            FakeResponse(200, {"ok": True, "result": {}}),
        ]
    )
    notifier = make_notifier(fake)
    item = make_item()

    await notifier.send_item(item)  # nessuna eccezione: la notifica parte comunque

    assert [url.rsplit("/", 1)[1] for url, _ in fake.calls] == ["sendPhoto", "sendMessage"]
    _, fallback_payload = fake.calls[1]
    assert fallback_payload["text"] == format_item_message(item)
    assert fallback_payload["parse_mode"] == "HTML"


# ------------------------------------------ (h) 401 → nessun fallback


async def test_auth_error_on_photo_does_not_fall_back(sleeps: list[float]) -> None:
    fake = FakeSession([FakeResponse(401, {"ok": False, "description": "Unauthorized"})])
    notifier = make_notifier(fake)

    with pytest.raises(TelegramError, match="token invalido"):
        await notifier.send_item(make_item())

    assert len(fake.calls) == 1  # solo sendPhoto: niente retry, niente fallback


# ------------------------------------------------- album (foto multiple)


async def test_multiple_photos_use_media_group_with_caption_on_first() -> None:
    fake = FakeSession([FakeResponse(200, {"ok": True, "result": []})])
    notifier = make_notifier(fake)
    item = make_item(n_photos=3)

    await notifier.send_item(item)

    url, payload = fake.calls[0]
    assert url.endswith("/sendMediaGroup")
    media = payload["media"]
    assert [m["media"] for m in media] == [
        f"https://images1.vinted.net/photo{i}.jpeg" for i in (1, 2, 3)
    ]
    assert media[0]["caption"] == format_item_message(item)
    assert media[0]["parse_mode"] == "HTML"
    assert "caption" not in media[1] and "caption" not in media[2]


async def test_album_capped_at_ten_photos() -> None:
    fake = FakeSession([FakeResponse(200, {"ok": True, "result": []})])
    notifier = make_notifier(fake)

    await notifier.send_item(make_item(n_photos=14))

    _, payload = fake.calls[0]
    assert len(payload["media"]) == 10  # limite Telegram


async def test_broken_album_photo_is_dropped_and_album_retried() -> None:
    fake = FakeSession(
        [
            FakeResponse(
                400,
                {"ok": False,
                 "description": 'Bad Request: failed to send message #2 with the error'
                                ' message "WEBPAGE_CURL_FAILED"'},
            ),
            FakeResponse(200, {"ok": True, "result": []}),
        ]
    )
    notifier = make_notifier(fake)
    item = make_item(n_photos=3)

    await notifier.send_item(item)

    # Ritenta l'ALBUM senza la foto #2, non degrada a foto singola.
    assert [url.rsplit("/", 1)[1] for url, _ in fake.calls] == ["sendMediaGroup", "sendMediaGroup"]
    _, retry_payload = fake.calls[1]
    assert [m["media"] for m in retry_payload["media"]] == [
        "https://images1.vinted.net/photo1.jpeg",
        "https://images1.vinted.net/photo3.jpeg",  # la #2 è stata scartata
    ]
    assert retry_payload["media"][0]["caption"] == format_item_message(item)


async def test_album_failure_without_media_index_degrades_to_single_photo() -> None:
    fake = FakeSession(
        [
            FakeResponse(400, {"ok": False, "description": "Bad Request: group send failed"}),
            FakeResponse(200, {"ok": True, "result": {}}),
        ]
    )
    notifier = make_notifier(fake)

    await notifier.send_item(make_item(n_photos=3))

    assert [url.rsplit("/", 1)[1] for url, _ in fake.calls] == ["sendMediaGroup", "sendPhoto"]
    _, photo_payload = fake.calls[1]
    assert photo_payload["photo"] == "https://images1.vinted.net/photo1.jpeg"  # la principale


async def test_album_then_photo_failure_degrades_to_text() -> None:
    fake = FakeSession(
        [
            FakeResponse(400, {"ok": False, "description": "Bad Request: group send failed"}),
            FakeResponse(
                400,
                {"ok": False,
                 "description": "Bad Request: wrong file identifier/HTTP URL specified"},
            ),
            FakeResponse(200, {"ok": True, "result": {}}),
        ]
    )
    notifier = make_notifier(fake)

    await notifier.send_item(make_item(n_photos=3))

    assert [url.rsplit("/", 1)[1] for url, _ in fake.calls] == [
        "sendMediaGroup", "sendPhoto", "sendMessage",
    ]


async def test_album_drop_budget_exhausted_degrades() -> None:
    def broken(n: int) -> FakeResponse:
        return FakeResponse(
            400,
            {"ok": False,
             "description": f'Bad Request: failed to send message #{n} with the error'
                            ' message "WEBPAGE_CURL_FAILED"'},
        )

    # 4 foto rotte di fila (> budget di 3 drop) → dopo i retry degrada a sendPhoto.
    fake = FakeSession(
        [broken(1), broken(1), broken(1), broken(1), FakeResponse(200, {"ok": True, "result": {}})]
    )
    notifier = make_notifier(fake)

    await notifier.send_item(make_item(n_photos=8))

    methods = [url.rsplit("/", 1)[1] for url, _ in fake.calls]
    assert methods == ["sendMediaGroup"] * 4 + ["sendPhoto"]


# ------------------------------------------- (i) senza foto → solo testo


async def test_item_without_photo_goes_straight_to_send_message() -> None:
    fake = FakeSession([FakeResponse(200, {"ok": True, "result": {}})])
    notifier = make_notifier(fake)
    item = make_item(with_photo=False)

    await notifier.send_item(item)

    assert len(fake.calls) == 1
    url, payload = fake.calls[0]
    assert url.endswith("/sendMessage")
    assert payload["text"] == format_item_message(item)
    assert payload["parse_mode"] == "HTML"
