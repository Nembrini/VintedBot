"""Tests for file logging, rotation, console gating and secret masking."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import pytest
import structlog

from vintedbot.config import Settings
from vintedbot.log import LOG_FILENAME, setup_logging, setup_logging_from_settings

if TYPE_CHECKING:
    from pathlib import Path

TOKEN = "123456789:AAfaketokenfaketokenfaketoken"


@pytest.fixture(autouse=True)
def _restore_logging() -> object:
    """Leave global logging/structlog as we found them."""
    yield
    for handler in list(logging.getLogger().handlers):
        logging.getLogger().removeHandler(handler)
        handler.close()
    structlog.reset_defaults()


def read_log(log_dir: Path) -> str:
    return (log_dir / LOG_FILENAME).read_text(encoding="utf-8")


# ------------------------------------------------------- (e) file e console


def test_log_file_is_created_and_written(tmp_path: Path) -> None:
    log_file = setup_logging("INFO", log_dir=tmp_path / "logs")

    structlog.get_logger("test").info("run_started", run_id="abc123", searches=["alfa"])

    assert log_file == tmp_path / "logs" / LOG_FILENAME
    content = read_log(tmp_path / "logs")
    assert "run_started" in content
    assert "abc123" in content


def test_non_tty_produces_no_console_output_and_no_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # capsys sostituisce stderr con un oggetto non-tty: è lo scenario
    # Task Scheduler.
    setup_logging("INFO", log_dir=tmp_path / "logs")
    structlog.get_logger("test").info("silenzioso")

    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""
    assert "silenzioso" in read_log(tmp_path / "logs")


def test_verbose_forces_console_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    setup_logging("INFO", log_dir=tmp_path / "logs", verbose=True)
    structlog.get_logger("test").info("visibile")

    assert "visibile" in capsys.readouterr().err


def test_rotation_keeps_backups(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    setup_logging("INFO", log_dir=log_dir, max_bytes=2048, backup_count=2)

    logger = structlog.get_logger("test")
    for index in range(400):
        logger.info("riempimento", index=index, filler="x" * 100)

    rotated = sorted(path.name for path in log_dir.iterdir())
    assert LOG_FILENAME in rotated
    assert f"{LOG_FILENAME}.1" in rotated  # la rotazione è avvenuta
    assert len(rotated) <= 3  # file corrente + backup_count


# ---------------------------------------------------------- (f) segreti


def test_token_never_reaches_the_log_even_through_tracebacks(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        data_dir=tmp_path,
        telegram_bot_token=TOKEN,
        telegram_chat_id="998877665",
    )
    setup_logging_from_settings(settings)
    logger = structlog.get_logger("test")

    # 1. il token in un campo strutturato
    logger.info("chiamata", url=f"https://api.telegram.org/bot{TOKEN}/sendMessage")
    # 2. il token dentro il messaggio dell'eccezione (è così che i client
    #    HTTP lo fanno trapelare: l'URL finisce nel traceback)
    try:
        raise ConnectionError(
            f"Failed to perform, curl: (7) https://api.telegram.org/bot{TOKEN}/sendPhoto"
        )
    except ConnectionError:
        logger.exception("richiesta_fallita")
    # 3. il chat_id
    logger.info("invio", chat_id="998877665")

    content = read_log(log_dir)
    assert TOKEN not in content
    assert "998877665" not in content
    assert content.count("***REDACTED***") >= 3
    assert "richiesta_fallita" in content  # il resto del log sopravvive


def test_short_chat_ids_are_not_masked(tmp_path: Path) -> None:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None, data_dir=tmp_path, telegram_bot_token=TOKEN, telegram_chat_id="42"
    )
    # Mascherare "42" ovunque rovinerebbe i log: gli id corti sono esclusi.
    assert settings.secret_values() == (TOKEN,)
