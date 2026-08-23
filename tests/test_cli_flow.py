"""Orchestration tests for the search command (cli.main → app.run_search).

HTTP client faked with the real fixture, DB on tmp_path — zero network.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from typing import TYPE_CHECKING, Any

import pytest

import vintedbot.app
import vintedbot.cli
from vintedbot.cli import main
from vintedbot.config import Settings
from vintedbot.models import Item, parse_items

if TYPE_CHECKING:
    from pathlib import Path


class FakeClient:
    """Stand-in for VintedClient: serves the fixture page, then an empty page."""

    def __init__(self, pages: dict[int, list[Item]]) -> None:
        self._pages = pages

    async def __aenter__(self) -> FakeClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def search(
        self, filters: object, page: int = 1, *, per_page: int = 96
    ) -> list[Item]:
        return self._pages.get(page, [])


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "vintedbot.db"


@pytest.fixture()
def fixture_items(catalog_page: dict[str, Any]) -> list[Item]:
    return parse_items(catalog_page["items"])


@pytest.fixture(autouse=True)
def _wire_fakes(
    monkeypatch: pytest.MonkeyPatch, db_path: Path, fixture_items: list[Item]
) -> None:
    """Point the app at a tmp DB and a fake client serving the fixture."""
    settings = Settings(_env_file=None, db_path=db_path)  # type: ignore[call-arg]
    monkeypatch.setattr(vintedbot.cli, "get_settings", lambda: settings)
    # setup_logging configurerebbe structlog sul sys.stderr catturato da
    # capsys e lo cacherebbe: i test successivi scriverebbero su un file
    # chiuso. Nei test il logging resta sulla configurazione di default.
    monkeypatch.setattr(vintedbot.cli, "setup_logging", lambda *a, **kw: None)
    monkeypatch.setattr(
        vintedbot.app, "VintedClient", lambda _settings: FakeClient({1: fixture_items})
    )


def _db_count(db_path: Path) -> int:
    with closing(sqlite3.connect(db_path)) as conn:
        return int(conn.execute("SELECT COUNT(*) FROM seen_items").fetchone()[0])


ARGS = ["search", "--catalog", "2536", "--size", "208", "--max-price", "20"]


# ------------------------------------------------- (a) prima esecuzione


def test_first_run_shows_everything_and_marks_seen(
    capsys: pytest.CaptureFixture[str], db_path: Path, fixture_items: list[Item]
) -> None:
    rc = main(ARGS)

    assert rc == 0
    out = capsys.readouterr().out
    assert f"{len(fixture_items)} nuovi" in out
    assert "0 già visti" in out
    assert "Risultati Vinted" in out  # la tabella è stata renderizzata
    assert _db_count(db_path) == len(fixture_items)


# ---------------------------------------------- (b) seconda esecuzione


def test_second_identical_run_shows_nothing_new(
    capsys: pytest.CaptureFixture[str], db_path: Path, fixture_items: list[Item]
) -> None:
    assert main(ARGS) == 0
    capsys.readouterr()  # scarta l'output della prima esecuzione

    rc = main(ARGS)

    assert rc == 0  # non è un errore: esito normale da schedulato
    out = capsys.readouterr().out
    assert "Nessun nuovo annuncio" in out
    assert f"({len(fixture_items)} già visti)" in out
    assert "Risultati Vinted" not in out  # niente tabella vuota
    assert _db_count(db_path) == len(fixture_items)  # count invariato


# --------------------------------------------------------- (c) --all


def test_all_flag_shows_everything_without_writing(
    capsys: pytest.CaptureFixture[str], db_path: Path, fixture_items: list[Item]
) -> None:
    assert main(ARGS) == 0  # popola il DB
    count_before = _db_count(db_path)
    capsys.readouterr()

    rc = main([*ARGS, "--all"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "Risultati Vinted" in out  # tabella con TUTTI gli item
    assert f"{len(fixture_items)} totali trovati" in out
    assert "filtro disattivato" in out
    assert _db_count(db_path) == count_before  # nessuna scrittura


# ------------------------------------------- (d) --purge-days invalido


@pytest.mark.parametrize("bad_days", ["0", "-5"])
def test_invalid_purge_days_fails_cleanly(
    capsys: pytest.CaptureFixture[str], db_path: Path, bad_days: str
) -> None:
    rc = main([*ARGS, "--purge-days", bad_days])

    assert rc != 0
    captured = capsys.readouterr()
    assert "--purge-days non valido" in captured.err
    assert "Traceback" not in captured.err + captured.out
    assert not db_path.exists() or _db_count(db_path) == 0  # niente ricerca eseguita


# ------------------------------------------ (e) rendering fallito


def test_failed_rendering_does_not_mark_items_seen(
    monkeypatch: pytest.MonkeyPatch, db_path: Path
) -> None:
    def exploding_render(console: object, items: list[Item]) -> None:
        raise RuntimeError("terminal on fire")

    monkeypatch.setattr(vintedbot.cli, "_render_items_table", exploding_render)

    with pytest.raises(RuntimeError, match="terminal on fire"):
        main(ARGS)

    # Crash PRIMA di mark_seen: nessun item bruciato, riappariranno.
    assert _db_count(db_path) == 0
