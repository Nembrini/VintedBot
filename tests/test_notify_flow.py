"""Orchestration tests for the notification phase of the search flow.

Everything faked: Vinted client on synthetic items, Telegram notifier as a
scriptable stub, DB in tmp_path, sleeps recorded — zero network.
"""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import closing
from typing import TYPE_CHECKING

import pytest

import vintedbot.app
import vintedbot.cli
from vintedbot.cli import main
from vintedbot.config import Settings
from vintedbot.models import Item
from vintedbot.notifier import TelegramError

if TYPE_CHECKING:
    from pathlib import Path

ARGS = ["search", "--catalog", "2536"]
TOKEN = "123456789:AAfaketokenfaketokenfaketoken"


def make_item(item_id: int) -> Item:
    return Item.model_validate(
        {
            "id": item_id,
            "title": f"item {item_id}",
            "price": {"amount": "10.0", "currency_code": "EUR"},
            "url": f"https://www.vinted.it/items/{item_id}",
            "user": {"id": 1, "login": "seller"},
            "photo": {"url": f"https://images1.vinted.net/{item_id}.jpeg"},
        }
    )


class FakeClient:
    def __init__(self, pages: dict[int, list[Item]]) -> None:
        self._pages = pages

    async def __aenter__(self) -> FakeClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def search(self, filters: object, page: int = 1, *, per_page: int = 96) -> list[Item]:
        return self._pages.get(page, [])


class FakeNotifier:
    """Scriptable TelegramNotifier stub shared across a test via class state."""

    sent_ids: list[int] = []
    failures: dict[int, Exception] = {}
    instantiated: int = 0

    def __init__(self, settings: object = None, **kwargs: object) -> None:
        FakeNotifier.instantiated += 1

    async def __aenter__(self) -> FakeNotifier:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def send_item(self, item: Item) -> None:
        failure = FakeNotifier.failures.get(item.id)
        if failure is not None:
            raise failure
        FakeNotifier.sent_ids.append(item.id)


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "vintedbot.db"


@pytest.fixture(autouse=True)
def _wire(monkeypatch: pytest.MonkeyPatch, db_path: Path) -> None:
    FakeNotifier.sent_ids = []
    FakeNotifier.failures = {}
    FakeNotifier.instantiated = 0

    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        db_path=db_path,
        telegram_bot_token=TOKEN,
        telegram_chat_id="42",
        notify_pause_seconds=1.0,
    )
    monkeypatch.setattr(vintedbot.cli, "get_settings", lambda: settings)
    monkeypatch.setattr(vintedbot.cli, "setup_logging", lambda *a, **kw: None)
    monkeypatch.setattr(vintedbot.app, "TelegramNotifier", FakeNotifier)

    async def fake_sleep(delay: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)


def set_pages(monkeypatch: pytest.MonkeyPatch, items: list[Item]) -> None:
    monkeypatch.setattr(
        vintedbot.app, "VintedClient", lambda _settings: FakeClient({1: items})
    )


def notified_map(db_path: Path) -> dict[int, str | None]:
    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        return {
            row["item_id"]: row["notified_at"]
            for row in conn.execute("SELECT item_id, notified_at FROM seen_items")
        }


# --------------------------------------------------------- (a) tutto ok


def test_all_sends_succeed(
    monkeypatch: pytest.MonkeyPatch, db_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    set_pages(monkeypatch, [make_item(1), make_item(2), make_item(3)])

    assert main(ARGS) == 0

    assert sorted(FakeNotifier.sent_ids) == [1, 2, 3]
    assert all(ts is not None for ts in notified_map(db_path).values())
    out = capsys.readouterr().out
    assert "3 notifiche inviate / 0 fallite" in out


# ---------------------------------- (b) fallimento singolo + recupero


def test_single_failure_is_retried_next_run(
    monkeypatch: pytest.MonkeyPatch, db_path: Path
) -> None:
    set_pages(monkeypatch, [make_item(1), make_item(2), make_item(3)])
    FakeNotifier.failures = {2: TelegramError("foto rotta", status_code=400,
                                              description="failed to get http url content")}

    assert main(ARGS) == 0

    state = notified_map(db_path)
    assert state[1] is not None and state[3] is not None
    assert state[2] is None  # fallito: resta in coda

    # Giro successivo: Vinted non dà nulla di nuovo, l'invio ora riesce.
    FakeNotifier.failures = {}
    FakeNotifier.sent_ids = []
    assert main(ARGS) == 0

    assert FakeNotifier.sent_ids == [2]  # SOLO l'arretrato
    assert all(ts is not None for ts in notified_map(db_path).values())


# ------------------------------------------- (c) 401 → coda interrotta


def test_fatal_config_error_aborts_queue(
    monkeypatch: pytest.MonkeyPatch, db_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    items = [make_item(1), make_item(2), make_item(3)]
    set_pages(monkeypatch, items)
    FakeNotifier.failures = {
        item.id: TelegramError("token invalido", status_code=401) for item in items
    }

    rc = main(ARGS)

    assert rc != 0
    assert FakeNotifier.sent_ids == []
    assert all(ts is None for ts in notified_map(db_path).values())
    assert "Notifiche interrotte" in capsys.readouterr().err


# ------------------------------------------------------ (d) --no-notify


def test_no_notify_flag_skips_telegram_entirely(
    monkeypatch: pytest.MonkeyPatch, db_path: Path
) -> None:
    set_pages(monkeypatch, [make_item(1), make_item(2)])

    assert main([*ARGS, "--no-notify"]) == 0

    assert FakeNotifier.instantiated == 0  # mai istanziato
    assert all(ts is None for ts in notified_map(db_path).values())


# -------------------------------------------- (e) credenziali mancanti


def test_missing_credentials_behave_like_no_notify(
    monkeypatch: pytest.MonkeyPatch, db_path: Path
) -> None:
    settings = Settings(_env_file=None, db_path=db_path)  # type: ignore[call-arg]
    monkeypatch.setattr(vintedbot.cli, "get_settings", lambda: settings)
    set_pages(monkeypatch, [make_item(1)])

    from structlog.testing import capture_logs

    with capture_logs() as logs:
        rc = main(ARGS)

    assert rc == 0
    assert FakeNotifier.instantiated == 0
    assert all(ts is None for ts in notified_map(db_path).values())
    assert any(entry["event"] == "telegram_not_configured" for entry in logs)


# ----------------------------------------------------- (f) anti-valanga


def test_notification_cap_and_drain_on_next_run(
    monkeypatch: pytest.MonkeyPatch, db_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    items = [make_item(i) for i in range(1, 16)]  # 15 nuovi, cap = 10
    set_pages(monkeypatch, items)

    assert main(ARGS) == 0

    state = notified_map(db_path)
    assert sum(ts is not None for ts in state.values()) == 10
    assert sum(ts is None for ts in state.values()) == 5
    assert "5 in coda per i prossimi giri" in capsys.readouterr().out

    # Giro successivo: i 5 rimasti vengono smaltiti.
    FakeNotifier.sent_ids = []
    assert main(ARGS) == 0
    assert len(FakeNotifier.sent_ids) == 5
    assert all(ts is not None for ts in notified_map(db_path).values())


# ------------------------------------------ (h) crash a metà coda


def test_crash_mid_queue_preserves_already_sent(
    monkeypatch: pytest.MonkeyPatch, db_path: Path
) -> None:
    set_pages(monkeypatch, [make_item(1), make_item(2), make_item(3)])

    crash_after = 2
    original_send = FakeNotifier.send_item

    async def crashing_send(self: FakeNotifier, item: Item) -> None:
        if len(FakeNotifier.sent_ids) >= crash_after:
            raise RuntimeError("processo morto a metà coda")
        await original_send(self, item)

    monkeypatch.setattr(FakeNotifier, "send_item", crashing_send)

    with pytest.raises(RuntimeError, match="metà coda"):
        main(ARGS)

    # I 2 inviati PRIMA del crash sono marcati (mark per singolo item),
    # il terzo resta NULL e ripartirà al giro successivo.
    state = notified_map(db_path)
    assert sum(ts is not None for ts in state.values()) == 2
    assert sum(ts is None for ts in state.values()) == 1
