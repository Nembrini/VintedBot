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


def _observations_count(db_path: Path) -> int:
    with closing(sqlite3.connect(db_path)) as conn:
        return int(conn.execute("SELECT COUNT(*) FROM price_observations").fetchone()[0])


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


# ------------------------------------- osservazioni prezzo (step 4.1)


def test_search_records_price_observations(
    capsys: pytest.CaptureFixture[str], db_path: Path, fixture_items: list[Item]
) -> None:
    assert main(ARGS) == 0

    # Una osservazione per item della fixture, attribuita al catalog 2536.
    assert _observations_count(db_path) == len(fixture_items)
    with closing(sqlite3.connect(db_path)) as conn:
        catalogs = {
            row[0] for row in conn.execute("SELECT DISTINCT catalog_id FROM price_observations")
        }
    assert catalogs == {2536}
    assert f"{len(fixture_items)} osservazioni prezzo registrate" in capsys.readouterr().out

    # Secondo run: gli item sono già visti ma i prezzi vengono ri-osservati.
    assert main(ARGS) == 0
    assert _observations_count(db_path) == 2 * len(fixture_items)


def test_all_flag_still_records_observations(
    capsys: pytest.CaptureFixture[str], db_path: Path, fixture_items: list[Item]
) -> None:
    assert main([*ARGS, "--all"]) == 0

    # --all: osservare non è notificare — prezzi registrati, seen_items intatto.
    assert _observations_count(db_path) == len(fixture_items)
    assert _db_count(db_path) == 0


def test_stats_command_reflects_recorded_data(
    capsys: pytest.CaptureFixture[str], db_path: Path
) -> None:
    assert main(ARGS) == 0
    capsys.readouterr()

    rc = main(["stats"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "Storico osservazioni prezzo" in out
    assert "2536" in out  # la categoria della ricerca
    assert "osservazioni totali" in out


def test_purge_days_also_purges_observations(db_path: Path) -> None:
    assert main(ARGS) == 0
    # invecchia artificialmente le osservazioni e i seen_items
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute("UPDATE price_observations SET observed_at = '2020-01-01T00:00:00+00:00'")
        conn.execute("UPDATE seen_items SET first_seen_at = '2020-01-01T00:00:00+00:00'")

    assert main([*ARGS, "--purge-days", "30"]) == 0

    # le vecchie osservazioni sono sparite; restano solo quelle del run appena fatto
    with closing(sqlite3.connect(db_path)) as conn:
        old = conn.execute(
            "SELECT COUNT(*) FROM price_observations WHERE observed_at < '2021-01-01'"
        ).fetchone()[0]
    assert old == 0


# ------------------------------------------------- backfill (step 4.2)


def test_backfill_records_only_observations(
    capsys: pytest.CaptureFixture[str], db_path: Path, fixture_items: list[Item]
) -> None:
    rc = main(["backfill", "--catalog", "2536", "--max-price", "20"])

    assert rc == 0
    assert _observations_count(db_path) == len(fixture_items)
    assert _db_count(db_path) == 0  # seen_items MAI toccato dal backfill
    captured = capsys.readouterr()
    assert "--max-price è IGNORATO" in captured.err  # warning esplicito
    assert "osservazioni nuove" in captured.out
    assert "Mediana" in captured.out  # snapshot per brand


# --------------------------------------- validazioni --min-score (l)


@pytest.mark.parametrize("bad", ["150", "-1"])
def test_min_score_out_of_range_fails_cleanly(
    capsys: pytest.CaptureFixture[str], bad: str
) -> None:
    rc = main([*ARGS, "--min-score", bad])

    assert rc != 0
    captured = capsys.readouterr()
    assert "--min-score" in captured.err
    assert "Traceback" not in captured.err + captured.out


def test_strict_score_without_min_score_fails(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = main([*ARGS, "--strict-score"])

    assert rc != 0
    assert "--strict-score richiede --min-score" in capsys.readouterr().err


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
    def exploding_render(console: object, items: list[Item], estimates: object) -> None:
        raise RuntimeError("terminal on fire")

    monkeypatch.setattr(vintedbot.cli, "_render_items_table", exploding_render)

    with pytest.raises(RuntimeError, match="terminal on fire"):
        main(ARGS)

    # Crash PRIMA di mark_seen: nessun item bruciato, riappariranno.
    assert _db_count(db_path) == 0
