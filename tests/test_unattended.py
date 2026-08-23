"""Integration tests for unattended `run-all`: lock, watchdog, exit codes,
data migration and the cloud-folder warning. Zero network."""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import closing
from typing import TYPE_CHECKING

import pytest

import vintedbot.app
import vintedbot.cli
import vintedbot.health
from vintedbot.cli import ExitCode, main
from vintedbot.config import Settings
from vintedbot.db import get_connection
from vintedbot.health import HealthState
from vintedbot.lock import SingleInstanceLock
from vintedbot.models import Item

if TYPE_CHECKING:
    from pathlib import Path

TOKEN = "123456789:AAfaketokenfaketokenfaketoken"

SEARCHES = """
[[search]]
name = "alfa"
catalog = 100

[[search]]
name = "beta"
catalog = 200
"""


def make_item(item_id: int) -> Item:
    return Item.model_validate(
        {
            "id": item_id,
            "title": f"item {item_id}",
            "price": {"amount": "10.0", "currency_code": "EUR"},
            "url": f"https://www.vinted.it/items/{item_id}",
            "user": {"id": 1, "login": "seller"},
        }
    )


class FakeClient:
    pages: dict[int, list[Item]] = {}
    searched: list[int] = []
    delay: float = 0.0

    def __init__(self, settings: object = None) -> None:
        pass

    async def __aenter__(self) -> FakeClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def search(self, filters: object, page: int = 1, *, per_page: int = 96) -> list[Item]:
        catalog = filters.category_ids[0] if filters.category_ids else 0  # type: ignore[attr-defined]
        if page != 1:
            return []
        FakeClient.searched.append(catalog)
        if FakeClient.delay:
            await asyncio.sleep(FakeClient.delay)
        return FakeClient.pages.get(catalog, [])


class FakeNotifier:
    sent_ids: list[int] = []
    status_messages: list[str] = []

    def __init__(self, settings: object = None, **kwargs: object) -> None:
        pass

    async def __aenter__(self) -> FakeNotifier:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def send_item(self, item: Item, estimate: object = None) -> None:
        FakeNotifier.sent_ids.append(item.id)

    async def send_text(self, text: str) -> None:
        FakeNotifier.status_messages.append(text)


@pytest.fixture()
def searches_file(tmp_path: Path) -> Path:
    path = tmp_path / "searches.toml"
    path.write_text(SEARCHES, encoding="utf-8")
    return path


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        data_dir=tmp_path / "data",
        telegram_bot_token=TOKEN,
        telegram_chat_id="424242",
        delay_between_searches_seconds=0,
        notify_pause_seconds=0,
    )


@pytest.fixture(autouse=True)
def _wire(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> None:
    FakeClient.pages = {100: [make_item(1)], 200: [make_item(2)]}
    FakeClient.searched = []
    FakeClient.delay = 0.0
    FakeNotifier.sent_ids = []
    FakeNotifier.status_messages = []

    monkeypatch.setattr(vintedbot.cli, "get_settings", lambda: settings)
    monkeypatch.setattr(
        vintedbot.cli, "setup_logging_from_settings", lambda *a, **kw: None
    )
    monkeypatch.setattr(vintedbot.app, "VintedClient", FakeClient)
    monkeypatch.setattr(vintedbot.app, "TelegramNotifier", FakeNotifier)
    monkeypatch.setattr(vintedbot.health, "TelegramNotifier", FakeNotifier)


# --------------------------------------------------------------- (a) lock


def test_second_instance_exits_with_locked_code(
    searches_file: Path, settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    with SingleInstanceLock(settings.lock_path):  # il "primo run" tiene il lock
        rc = main(["run-all", "--searches", str(searches_file)])

    assert rc == ExitCode.LOCKED
    assert FakeClient.searched == []  # non ha toccato nulla
    assert FakeNotifier.status_messages == []  # nessuna notifica di guasto
    assert "già in esecuzione" in capsys.readouterr().out


# ------------------------------------------------- (b) nessun lock orfano


def test_run_after_a_crash_is_not_blocked(
    searches_file: Path, settings: Settings
) -> None:
    import os

    crashed = SingleInstanceLock(settings.lock_path)
    crashed.__enter__()
    os.close(crashed._fd)  # type: ignore[arg-type]  # noqa: SLF001 — morte anomala
    crashed._fd = None  # noqa: SLF001

    assert main(["run-all", "--searches", str(searches_file)]) == ExitCode.OK
    assert FakeClient.searched == [100, 200]


# ------------------------------------------------------- (c) --ignore-lock


def test_ignore_lock_forces_execution(
    searches_file: Path, settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    with SingleInstanceLock(settings.lock_path):
        rc = main(["run-all", "--searches", str(searches_file), "--ignore-lock"])

    assert rc == ExitCode.OK
    assert FakeClient.searched == [100, 200]
    assert "solo per debug" in capsys.readouterr().out


# ------------------------------------------------------------ (d) watchdog


def test_watchdog_stops_the_run_and_reports_skipped(
    monkeypatch: pytest.MonkeyPatch,
    searches_file: Path,
    settings: Settings,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(settings, "max_run_seconds", 0.05)
    FakeClient.delay = 0.2  # la prima ricerca sfora da sola

    rc = main(["run-all", "--searches", str(searches_file)])

    assert rc == ExitCode.TIMEOUT
    err = capsys.readouterr().err
    assert "Watchdog" in err
    assert "alfa" in err and "beta" in err  # nomina le ricerche non eseguite
    # interruzione pulita: il DB è integro e interrogabile
    with closing(get_connection(settings.db_path)) as conn:
        conn.execute("SELECT COUNT(*) FROM seen_items").fetchone()


def test_watchdog_skips_remaining_searches_between_runs(
    monkeypatch: pytest.MonkeyPatch, searches_file: Path, settings: Settings
) -> None:
    # deadline che scade DOPO la prima ricerca: la seconda non parte
    monkeypatch.setattr(settings, "max_run_seconds", 0.12)
    FakeClient.delay = 0.1

    rc = main(["run-all", "--searches", str(searches_file)])

    assert rc == ExitCode.TIMEOUT
    assert FakeClient.searched == [100]


# ---------------------------------------------------------- (g) exit code


def test_exit_code_ok(searches_file: Path) -> None:
    assert main(["run-all", "--searches", str(searches_file)]) == ExitCode.OK


def test_exit_code_config_on_invalid_file(tmp_path: Path) -> None:
    bad = tmp_path / "bad.toml"
    bad.write_text('[[search]]\nname = "x"\nsconosciuto = 1\n', encoding="utf-8")
    assert main(["run-all", "--searches", str(bad)]) == ExitCode.CONFIG


def test_exit_code_error_when_a_search_fails(
    monkeypatch: pytest.MonkeyPatch, searches_file: Path
) -> None:
    from vintedbot.client import VintedError

    async def failing_search(self: FakeClient, filters: object, page: int = 1, **kw: object):
        raise VintedError("rete KO")

    monkeypatch.setattr(FakeClient, "search", failing_search)

    assert main(["run-all", "--searches", str(searches_file)]) == ExitCode.ERROR


def test_exit_code_error_on_unexpected_crash(
    monkeypatch: pytest.MonkeyPatch, searches_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    async def exploding(*args: object, **kwargs: object):
        raise ZeroDivisionError("bug inatteso")

    monkeypatch.setattr(vintedbot.cli, "run_all", exploding)

    rc = main(["run-all", "--searches", str(searches_file)])

    assert rc == ExitCode.ERROR
    assert "Esecuzione fallita" in capsys.readouterr().err
    # il guasto inatteso viene notificato (una volta)
    assert any("esecuzione fallita" in msg for msg in FakeNotifier.status_messages)


# ------------------------------------------------------- (k) migrate-data


def test_migrate_data_copies_wal_contents_and_verifies_counts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    settings: Settings,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from vintedbot.repository import ItemRepository, PriceRepository

    # DB di origine con dati ancora nel WAL (nessun checkpoint esplicito)
    source = tmp_path / "onedrive" / "vintedbot.db"
    conn = get_connection(source)
    ItemRepository(conn).mark_seen([make_item(1), make_item(2)])
    PriceRepository(conn).record_observations([make_item(1)], catalog_id=100)
    # NB: la connessione resta APERTA, il -wal non è stato consolidato
    monkeypatch.setattr(settings, "db_path", source)

    destination = tmp_path / "appdata" / "vintedbot.db"
    rc = main(["migrate-data", "--to", str(destination)])
    conn.close()

    assert rc == ExitCode.OK
    out = capsys.readouterr().out
    assert "seen_items: 2 → 2" in out
    assert "price_observations: 1 → 1" in out
    assert "VINTEDBOT_DB_PATH=" in out

    with closing(sqlite3.connect(destination)) as copy:
        assert copy.execute("SELECT COUNT(*) FROM seen_items").fetchone()[0] == 2
        assert copy.execute("SELECT COUNT(*) FROM price_observations").fetchone()[0] == 1
    assert source.exists()  # l'originale resta al suo posto


def test_migrate_data_refuses_to_overwrite(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, settings: Settings
) -> None:
    source = tmp_path / "src.db"
    get_connection(source).close()
    monkeypatch.setattr(settings, "db_path", source)
    destination = tmp_path / "dest.db"
    destination.write_text("non toccarmi", encoding="utf-8")

    assert main(["migrate-data", "--to", str(destination)]) == ExitCode.CONFIG
    assert destination.read_text(encoding="utf-8") == "non toccarmi"


# ------------------------------------------------- (l) warning OneDrive


def test_warning_when_db_lives_in_a_synced_folder(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, settings: Settings,
    searches_file: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(settings, "db_path", tmp_path / "OneDrive" / "Desktop" / "v.db")

    main(["run-all", "--searches", str(searches_file)])

    err = capsys.readouterr().err
    assert "cartella sincronizzata" in err and "onedrive" in err


def test_no_warning_outside_synced_folders(
    settings: Settings, searches_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    main(["run-all", "--searches", str(searches_file)])
    assert "cartella sincronizzata" not in capsys.readouterr().err


# ------------------------------------- (h) provenienza e traccia dell'ultimo run


def read_last_run(settings: Settings) -> dict[str, object]:
    return HealthState.load(settings.health_path).last_run


def test_run_records_its_outcome_for_doctor(
    searches_file: Path, settings: Settings
) -> None:
    assert main(["run-all", "--searches", str(searches_file)]) == ExitCode.OK

    last = read_last_run(settings)
    assert last["outcome"] == "ok"
    assert last["exit_code"] == 0
    assert last["trigger"] == "manual"
    assert last["notified"] == 2
    assert last["finished_at"]


def test_scheduler_runs_are_distinguishable_from_manual_ones(
    monkeypatch: pytest.MonkeyPatch, searches_file: Path, settings: Settings
) -> None:
    monkeypatch.setenv("VINTEDBOT_INVOKED_BY", "scheduler")

    assert main(["run-all", "--searches", str(searches_file)]) == ExitCode.OK

    assert read_last_run(settings)["trigger"] == "scheduler"


def test_timeout_is_recorded_with_its_exit_code(
    monkeypatch: pytest.MonkeyPatch, searches_file: Path, settings: Settings
) -> None:
    monkeypatch.setattr(settings, "max_run_seconds", 0.05)
    FakeClient.delay = 0.2

    assert main(["run-all", "--searches", str(searches_file)]) == ExitCode.TIMEOUT

    last = read_last_run(settings)
    assert last["outcome"] == "timeout"
    assert last["exit_code"] == 4


def test_a_skipped_run_is_not_recorded_as_a_run(
    searches_file: Path, settings: Settings
) -> None:
    """Exit 3 means "nothing happened": it must not overwrite the last real run."""
    assert main(["run-all", "--searches", str(searches_file)]) == ExitCode.OK
    genuine = read_last_run(settings)

    with SingleInstanceLock(settings.lock_path):
        assert main(["run-all", "--searches", str(searches_file)]) == ExitCode.LOCKED

    assert read_last_run(settings) == genuine
