"""`vintedbot doctor`: one screen that must never itself be the thing that breaks.

The Task Scheduler is never really queried — :func:`vintedbot.cli.task_status`
is replaced in every test.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from typing import TYPE_CHECKING

import pytest

import vintedbot.cli
from vintedbot.cli import ExitCode, main
from vintedbot.config import Settings
from vintedbot.db import get_connection
from vintedbot.lock import SingleInstanceLock
from vintedbot.schedule import SchedulerError, TaskStatus

if TYPE_CHECKING:
    from pathlib import Path

TOKEN = "123456789:AAfaketokenfaketokenfaketoken"
CHAT_ID = "998877665"

SEARCHES = """
[[search]]
name = "alfa"
catalog = 100
"""


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    (tmp_path / "searches.toml").write_text(SEARCHES, encoding="utf-8")
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        data_dir=tmp_path / "data",
        searches_path=tmp_path / "searches.toml",
        telegram_bot_token=TOKEN,
        telegram_chat_id=CHAT_ID,
    )


@pytest.fixture(autouse=True)
def _wire(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> None:
    monkeypatch.setattr(vintedbot.cli, "get_settings", lambda: settings)
    monkeypatch.setattr(vintedbot.cli, "setup_logging_from_settings", lambda *a, **kw: None)
    monkeypatch.setattr(vintedbot.cli, "task_status", lambda name: TaskStatus(registered=False))


def run_doctor(capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
    """Run doctor and flatten the output: rich wraps table cells at will."""
    code = main(["doctor"])
    return code, " ".join(capsys.readouterr().out.split())


def test_fresh_install_reports_gaps_without_failing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Nothing set up yet is a to-do list, not a breakage: exit 0."""
    code, out = run_doctor(capsys)

    assert code == ExitCode.OK
    assert "assente" in out  # database
    assert "non registrato" in out  # schedulazione
    assert "nessuna esecuzione registrata" in out


def test_reports_database_size_and_schema(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    with closing(get_connection(settings.db_path)):
        pass

    code, out = run_doctor(capsys)

    assert code == ExitCode.OK
    assert "schema v" in out
    assert "seen_items 0" in out


def test_corrupt_database_is_reported_not_raised(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    settings.db_path.write_bytes(b"questo non e' un database sqlite")

    code, out = run_doctor(capsys)

    assert code == ExitCode.ERROR
    assert "danneggiato" in out or "illeggibile" in out
    assert "Da sistemare" in out


def test_does_not_migrate_the_database(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    """A diagnosis must photograph the state, not change it."""
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(settings.db_path)) as conn:
        conn.execute("PRAGMA user_version = 0")

    run_doctor(capsys)

    with closing(sqlite3.connect(settings.db_path)) as conn:
        assert int(conn.execute("PRAGMA user_version").fetchone()[0]) == 0


def test_lock_free_and_file_not_created(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out = run_doctor(capsys)

    assert code == ExitCode.OK
    assert "libero" in out
    assert not settings.lock_path.exists()  # la diagnosi non lascia tracce


def test_lock_held_names_the_holder(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    import os

    with SingleInstanceLock(settings.lock_path):
        code, out = run_doctor(capsys)

    assert code == ExitCode.OK
    assert f"pid {os.getpid()}" in out


def test_probe_leaves_the_holder_payload_untouched(settings: Settings) -> None:
    """doctor must not overwrite the lock diagnostics with its own pid."""
    with SingleInstanceLock(settings.lock_path):
        pass
    before = settings.lock_path.read_bytes()

    main(["doctor"])

    assert settings.lock_path.read_bytes() == before


def test_last_run_comes_from_health_state(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    settings.health_path.parent.mkdir(parents=True, exist_ok=True)
    settings.health_path.write_text(
        json.dumps(
            {
                "consecutive_failures": 0,
                "notified_at": {},
                "last_run": {
                    "finished_at": "2026-08-23T15:32:00+00:00",
                    "outcome": "ok",
                    "exit_code": 0,
                    "duration_seconds": 14.0,
                    "trigger": "scheduler",
                    "notified": 7,
                },
            }
        ),
        encoding="utf-8",
    )

    code, out = run_doctor(capsys)

    assert code == ExitCode.OK
    assert "esito ok" in out
    assert "exit 0" in out
    assert "avvio scheduler" in out
    assert "7 notificati" in out


def test_damaged_health_file_does_not_crash(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    settings.health_path.parent.mkdir(parents=True, exist_ok=True)
    settings.health_path.write_text("{ rotto", encoding="utf-8")

    code, out = run_doctor(capsys)

    assert code == ExitCode.OK
    assert "nessuna esecuzione registrata" in out


def test_scheduler_unreachable_is_a_warning_not_a_crash(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom(name: str) -> TaskStatus:
        raise SchedulerError("servizio Utilità di pianificazione non disponibile")

    monkeypatch.setattr(vintedbot.cli, "task_status", boom)

    code, out = run_doctor(capsys)

    assert code == ExitCode.OK
    assert "non interrogabile" in out


def test_registered_task_is_summarised(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        vintedbot.cli,
        "task_status",
        lambda name: TaskStatus(
            registered=True,
            state="Ready",
            last_run_time="2026-08-23 15:30:00",
            last_result=3,
            next_run_time="2026-08-23 15:40:00",
        ),
    )

    code, out = run_doctor(capsys)

    assert code == ExitCode.OK
    assert "Ready" in out
    # exit 3 non e' un guasto e il testo deve dirlo
    assert "non è un guasto" in out


def test_invalid_searches_file_is_an_error(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    settings.searches_path.write_text("[[search]]\nname = 12\n", encoding="utf-8")

    code, out = run_doctor(capsys)

    assert code == ExitCode.ERROR
    assert "Da sistemare" in out


def test_credentials_are_never_printed(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out = run_doctor(capsys)

    assert code == ExitCode.OK
    assert "token e chat id configurati" in out
    assert TOKEN not in out
    assert CHAT_ID not in out


def test_missing_credentials_are_flagged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    bare = Settings(  # type: ignore[call-arg]
        _env_file=None,
        data_dir=tmp_path / "data",
        searches_path=tmp_path / "searches.toml",
    )
    monkeypatch.setattr(vintedbot.cli, "get_settings", lambda: bare)

    code, out = run_doctor(capsys)

    assert code == ExitCode.OK
    assert "mancano: token, chat id" in out
