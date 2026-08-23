"""Structured logging: always to a rotating file, to the console only when watched.

Under Task Scheduler there is no console at all, so console output is
opt-in (a TTY, or ``--verbose``) while the file handler is unconditional:
the log file is the only record of what an unattended run did.

structlog is routed through stdlib logging (``ProcessorFormatter``) so
both handlers share one processor chain — and, crucially, one secret
masking step. Masking runs AFTER ``format_exc_info``, so a bot token that
leaks inside an exception's rendered traceback (transport errors love to
embed the request URL) is redacted too.
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from pathlib import Path

    from vintedbot.config import LogLevel, Settings

REDACTED = "***REDACTED***"

#: Log file inside ``settings.log_dir``.
LOG_FILENAME = "vintedbot.log"


def mask_secrets(secrets: tuple[str, ...]) -> structlog.typing.Processor:
    """Processor replacing every known secret in every rendered string.

    Values are matched literally in all string fields of the event dict —
    including ``exception`` (the rendered traceback) and ``event`` itself.
    """

    def processor(
        logger: object, method_name: str, event_dict: structlog.typing.EventDict
    ) -> structlog.typing.EventDict:
        if not secrets:
            return event_dict
        for key, value in event_dict.items():
            if isinstance(value, str):
                masked = value
                for secret in secrets:
                    if secret and secret in masked:
                        masked = masked.replace(secret, REDACTED)
                if masked is not value:
                    event_dict[key] = masked
        return event_dict

    return processor


def setup_logging(
    level: LogLevel = "INFO",
    *,
    json_output: bool = False,
    log_dir: Path | None = None,
    secrets: tuple[str, ...] = (),
    verbose: bool = False,
    max_bytes: int = 5 * 1024 * 1024,
    backup_count: int = 5,
) -> Path | None:
    """Configure structlog + stdlib logging; returns the log file path, if any.

    Args:
        level: minimum level for all loggers.
        json_output: one JSON object per line instead of the console format.
        log_dir: when given, logs are written to ``<log_dir>/vintedbot.log``
            with size-based rotation.
        secrets: values to redact from every log line.
        verbose: force console output even without a TTY.
        max_bytes / backup_count: rotation policy for the file handler.
    """
    numeric_level = logging.getLevelNamesMapping()[level]

    shared_processors: list[structlog.typing.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.format_exc_info,
        mask_secrets(secrets),  # DOPO format_exc_info: maschera anche i traceback
    ]

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=False,
    )

    def make_formatter(*, colors: bool) -> logging.Formatter:
        renderer: structlog.typing.Processor = (
            structlog.processors.JSONRenderer()
            if json_output
            else structlog.dev.ConsoleRenderer(colors=colors)
        )
        return structlog.stdlib.ProcessorFormatter(
            processor=renderer,
            # I log emessi da librerie terze passano dalla stessa catena,
            # mascheramento incluso.
            foreign_pre_chain=shared_processors,
        )

    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    root.setLevel(numeric_level)

    log_file: Path | None = None
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / LOG_FILENAME
        file_handler = RotatingFileHandler(
            log_file,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        file_handler.setFormatter(make_formatter(colors=False))
        root.addHandler(file_handler)

    if verbose or sys.stderr.isatty():
        console_handler = logging.StreamHandler(sys.stderr)
        console_handler.setFormatter(make_formatter(colors=sys.stderr.isatty()))
        root.addHandler(console_handler)
    elif log_file is None:
        # Nessun file e nessuna console: evita il warning "no handlers".
        root.addHandler(logging.NullHandler())

    return log_file


def setup_logging_from_settings(
    settings: Settings, *, to_file: bool = True, verbose: bool = False
) -> Path | None:
    """Convenience wrapper wiring :func:`setup_logging` to the app settings."""
    return setup_logging(
        settings.log_level,
        json_output=settings.log_json,
        log_dir=settings.log_dir if to_file else None,
        secrets=settings.secret_values(),
        verbose=verbose,
        max_bytes=settings.log_max_bytes,
        backup_count=settings.log_backup_count,
    )


def bind_run_context(**values: Any) -> None:
    """Attach values (e.g. the run id) to every log line of this execution."""
    structlog.contextvars.bind_contextvars(**values)
