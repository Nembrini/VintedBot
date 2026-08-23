"""Orchestration tests for `run-all`: sequencing, global dedup, shared budget.

Everything faked: Vinted client per search, Telegram stub, DB in tmp_path,
sleeps recorded — zero network.
"""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import closing
from typing import TYPE_CHECKING

import pytest

import vintedbot.app
import vintedbot.cli
import vintedbot.health
from vintedbot.cli import main
from vintedbot.client import VintedError
from vintedbot.config import Settings
from vintedbot.models import Item
from vintedbot.notifier import TelegramError

if TYPE_CHECKING:
    from pathlib import Path

TOKEN = "123456789:AAfaketokenfaketokenfaketoken"


def make_item(item_id: int, price: str = "10.0") -> Item:
    return Item.model_validate(
        {
            "id": item_id,
            "title": f"item {item_id}",
            "price": {"amount": price, "currency_code": "EUR"},
            "url": f"https://www.vinted.it/items/{item_id}",
            "user": {"id": 1, "login": "seller"},
            "photo": {"url": f"https://images1.vinted.net/{item_id}.jpeg"},
        }
    )


class FakeClient:
    """Serves a scripted page per (catalog) so each saved search sees its own items."""

    pages_by_catalog: dict[int, list[Item]] = {}
    failing_catalogs: set[int] = set()
    searched: list[int] = []

    def __init__(self, settings: object = None) -> None:
        pass

    async def __aenter__(self) -> FakeClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def search(self, filters: object, page: int = 1, *, per_page: int = 96) -> list[Item]:
        catalog = filters.category_ids[0] if filters.category_ids else 0  # type: ignore[attr-defined]
        if page == 1:
            FakeClient.searched.append(catalog)
        if catalog in FakeClient.failing_catalogs:
            raise VintedError(f"rete KO per catalog {catalog}")
        return FakeClient.pages_by_catalog.get(catalog, []) if page == 1 else []


class FakeNotifier:
    sent_ids: list[int] = []
    status_messages: list[str] = []
    failures: dict[int, Exception] = {}
    instantiated: int = 0

    def __init__(self, settings: object = None, **kwargs: object) -> None:
        FakeNotifier.instantiated += 1

    async def __aenter__(self) -> FakeNotifier:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def send_item(self, item: Item, estimate: object = None) -> None:
        failure = FakeNotifier.failures.get(item.id)
        if failure is not None:
            raise failure
        FakeNotifier.sent_ids.append(item.id)

    async def send_text(self, text: str) -> None:
        FakeNotifier.status_messages.append(text)


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "vintedbot.db"


@pytest.fixture()
def searches_file(tmp_path: Path) -> Path:
    return tmp_path / "searches.toml"


@pytest.fixture(autouse=True)
def _wire(monkeypatch: pytest.MonkeyPatch, db_path: Path) -> list[float]:
    FakeClient.pages_by_catalog = {}
    FakeClient.failing_catalogs = set()
    FakeClient.searched = []
    FakeNotifier.sent_ids = []
    FakeNotifier.status_messages = []
    FakeNotifier.failures = {}
    FakeNotifier.instantiated = 0

    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        db_path=db_path,
        data_dir=db_path.parent,
        telegram_bot_token=TOKEN,
        telegram_chat_id="42",
        notify_pause_seconds=1.0,
        delay_between_searches_seconds=5.0,
    )
    monkeypatch.setattr(vintedbot.cli, "get_settings", lambda: settings)
    monkeypatch.setattr(
        vintedbot.cli, "setup_logging_from_settings", lambda *a, **kw: None
    )
    monkeypatch.setattr(vintedbot.app, "VintedClient", FakeClient)
    monkeypatch.setattr(vintedbot.app, "TelegramNotifier", FakeNotifier)
    # health.py ha il suo import: senza questo patch i messaggi di stato
    # tenterebbero una chiamata REALE ad api.telegram.org.
    monkeypatch.setattr(vintedbot.health, "TelegramNotifier", FakeNotifier)

    slept: list[float] = []

    async def fake_sleep(delay: float) -> None:
        slept.append(delay)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    return slept


def write_searches(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


def notified_ids(db_path: Path) -> set[int]:
    with closing(sqlite3.connect(db_path)) as conn:
        return {
            row[0]
            for row in conn.execute("SELECT item_id FROM seen_items WHERE notified_at IS NOT NULL")
        }


def db_counts(db_path: Path) -> tuple[int, int]:
    if not db_path.exists():
        return 0, 0
    with closing(sqlite3.connect(db_path)) as conn:
        seen = conn.execute("SELECT COUNT(*) FROM seen_items").fetchone()[0]
        obs = conn.execute("SELECT COUNT(*) FROM price_observations").fetchone()[0]
    return int(seen), int(obs)


TWO_SEARCHES = """
[[search]]
name = "alfa"
catalog = 100

[[search]]
name = "beta"
catalog = 200

[[search]]
name = "spenta"
enabled = false
catalog = 300
"""


# --------------------------------------------- (a) esegue solo le abilitate


def test_runs_only_enabled_searches_in_sequence(
    searches_file: Path, capsys: pytest.CaptureFixture[str], _wire: list[float]
) -> None:
    write_searches(searches_file, TWO_SEARCHES)
    FakeClient.pages_by_catalog = {100: [make_item(1)], 200: [make_item(2)], 300: [make_item(3)]}

    rc = main(["run-all", "--searches", str(searches_file)])

    assert rc == 0
    assert FakeClient.searched == [100, 200]  # 'spenta' saltata, ordine di file
    assert sorted(FakeNotifier.sent_ids) == [1, 2]
    out = capsys.readouterr().out
    assert "alfa" in out and "beta" in out and "TOTALE" in out


# --------------------------------------------------- (j) pause tra ricerche


def test_pause_between_searches(searches_file: Path, _wire: list[float]) -> None:
    write_searches(searches_file, TWO_SEARCHES)
    FakeClient.pages_by_catalog = {100: [], 200: []}

    assert main(["run-all", "--searches", str(searches_file)]) == 0

    # una sola pausa da 5s: tra la prima e la seconda ricerca (niente prima
    # della prima, niente dopo l'ultima); nessun invio → nessuna pausa da 1s
    assert _wire == [5.0]


# ------------------------------------------------------ (d) dedup globale


def test_overlapping_searches_notify_the_item_once(
    searches_file: Path, db_path: Path
) -> None:
    write_searches(searches_file, TWO_SEARCHES)
    shared = make_item(42)
    FakeClient.pages_by_catalog = {100: [shared, make_item(1)], 200: [shared, make_item(2)]}

    assert main(["run-all", "--searches", str(searches_file)]) == 0

    assert FakeNotifier.sent_ids.count(42) == 1  # seen_items è GLOBALE per id
    assert sorted(FakeNotifier.sent_ids) == [1, 2, 42]


# ------------------------------- (e) anti-valanga sull'INTERA esecuzione


def test_notification_budget_is_shared_across_searches(
    monkeypatch: pytest.MonkeyPatch, searches_file: Path, db_path: Path
) -> None:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None, db_path=db_path, data_dir=db_path.parent,
        telegram_bot_token=TOKEN, telegram_chat_id="42",
        max_notifications_per_run=3,
    )
    monkeypatch.setattr(vintedbot.cli, "get_settings", lambda: settings)
    write_searches(searches_file, TWO_SEARCHES)
    FakeClient.pages_by_catalog = {
        100: [make_item(i) for i in range(1, 6)],
        200: [make_item(i) for i in range(11, 16)],
    }

    assert main(["run-all", "--searches", str(searches_file)]) == 0

    # 3 in totale, NON 3 per ricerca
    assert len(FakeNotifier.sent_ids) == 3
    with closing(sqlite3.connect(db_path)) as conn:
        queued = conn.execute(
            "SELECT COUNT(*) FROM seen_items WHERE notified_at IS NULL AND skipped_at IS NULL"
        ).fetchone()[0]
    assert queued == 7  # gli altri restano in coda


# ------------------------------------- (f) una ricerca fallisce, le altre no


def test_failing_search_does_not_stop_the_others(
    searches_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_searches(searches_file, TWO_SEARCHES)
    FakeClient.pages_by_catalog = {200: [make_item(2)]}
    FakeClient.failing_catalogs = {100}

    rc = main(["run-all", "--searches", str(searches_file)])

    assert rc != 0  # almeno una fallita
    assert FakeNotifier.sent_ids == [2]  # la seconda ha lavorato lo stesso
    out = capsys.readouterr().out
    assert "errore" in out and "1 fallite" in out


# ------------------------------------------- (g) 401 Telegram: stop totale


def test_fatal_telegram_error_aborts_everything(
    searches_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_searches(searches_file, TWO_SEARCHES)
    FakeClient.pages_by_catalog = {100: [make_item(1)], 200: [make_item(2)]}
    FakeNotifier.failures = {1: TelegramError("token invalido", status_code=401)}

    rc = main(["run-all", "--searches", str(searches_file)])

    assert rc != 0
    assert FakeClient.searched == [100]  # la seconda ricerca non parte nemmeno
    assert "Esecuzione interrotta" in capsys.readouterr().err


# ----------------------------------------------------------- (h) --only


def test_only_runs_the_named_search(searches_file: Path) -> None:
    write_searches(searches_file, TWO_SEARCHES)
    FakeClient.pages_by_catalog = {100: [make_item(1)], 200: [make_item(2)]}

    assert main(["run-all", "--searches", str(searches_file), "--only", "beta"]) == 0

    assert FakeClient.searched == [200]
    assert FakeNotifier.sent_ids == [2]


def test_only_is_repeatable_and_keeps_file_order(searches_file: Path) -> None:
    write_searches(searches_file, TWO_SEARCHES)
    FakeClient.pages_by_catalog = {100: [make_item(1)], 200: [make_item(2)], 300: [make_item(3)]}

    # nomi passati in ordine inverso: si esegue comunque nell'ordine del file
    rc = main(
        ["run-all", "--searches", str(searches_file), "--only", "spenta", "--only", "alfa"]
    )

    assert rc == 0
    assert FakeClient.searched == [100, 300]
    assert sorted(FakeNotifier.sent_ids) == [1, 3]


def test_only_with_unknown_name_is_a_readable_error(
    searches_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_searches(searches_file, TWO_SEARCHES)

    rc = main(
        ["run-all", "--searches", str(searches_file), "--only", "alfa", "--only", "inesistente"]
    )

    assert rc != 0
    err = capsys.readouterr().err
    assert "inesistente" in err and "'alfa'" in err  # elenca le disponibili
    assert FakeClient.searched == []  # nemmeno la parte valida viene eseguita


def test_only_can_run_a_disabled_search(searches_file: Path) -> None:
    write_searches(searches_file, TWO_SEARCHES)
    FakeClient.pages_by_catalog = {300: [make_item(3)]}

    assert main(["run-all", "--searches", str(searches_file), "--only", "spenta"]) == 0

    assert FakeClient.searched == [300]


# ---------------------------------------------------------- (i) --dry-run


def test_dry_run_notifies_nothing_but_records_observations(
    searches_file: Path, db_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_searches(searches_file, TWO_SEARCHES)
    FakeClient.pages_by_catalog = {100: [make_item(1)], 200: [make_item(2)]}

    rc = main(["run-all", "--searches", str(searches_file), "--dry-run"])

    assert rc == 0
    assert FakeNotifier.instantiated == 0  # zero Telegram
    seen, observations = db_counts(db_path)
    assert seen == 0            # seen_items invariato
    assert observations == 2    # price_observations popolata comunque
    assert "dry-run" in capsys.readouterr().out


# --------------------------------------- (b/c) configurazione non valida


def test_invalid_config_exits_without_touching_the_network(
    searches_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_searches(searches_file, '[[search]]\nname = "x"\nmin_scor = 60\n')

    rc = main(["run-all", "--searches", str(searches_file)])

    assert rc != 0
    assert "campo sconosciuto" in capsys.readouterr().err
    assert FakeClient.searched == []


def test_missing_config_file_exits_cleanly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = main(["run-all", "--searches", str(tmp_path / "assente.toml")])

    assert rc != 0
    captured = capsys.readouterr()
    assert "searches.example.toml" in captured.err
    assert "Traceback" not in captured.err + captured.out
