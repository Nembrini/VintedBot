"""Structured logging setup built on structlog.

Call :func:`setup_logging` once at process start; afterwards obtain loggers
anywhere with ``structlog.get_logger(__name__)``.
"""

from __future__ import annotations

import logging
import sys
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from vintedbot.config import LogLevel


def setup_logging(level: LogLevel = "INFO", *, json_output: bool = False) -> None:
    """Configure structlog + stdlib logging.

    Args:
        level: minimum level for all loggers.
        json_output: if True emit one JSON object per line (machine-readable);
            otherwise a colored, human-friendly console format.
    """
    renderer: structlog.typing.Processor
    if json_output:
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer()

    shared_processors: list[structlog.typing.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.TimeStamper(fmt="iso", utc=True),
    ]

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping()[level]
        ),
        logger_factory=structlog.PrintLoggerFactory(sys.stderr),
        cache_logger_on_first_use=True,
    )

    # Route stdlib logging (used by third-party libs) through the same level.
    logging.basicConfig(level=level, stream=sys.stderr, format="%(message)s")
