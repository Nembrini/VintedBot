"""SQLite access layer: open/create the database file and migrate its schema.

Deliberately the standard-library ``sqlite3`` module — no ORM (unjustified
overhead at this scope) and no aiosqlite (operations are local and fast;
an async HTTP client does not force an async DB).

Schema versioning uses ``PRAGMA user_version``: :data:`MIGRATIONS` is an
ordered list and ``user_version`` counts how many entries have been
applied. Opening the same file any number of times is a safe no-op; new
tables (e.g. price history) will land as new list entries, never by
recreating the DB.

Conversions are ours, on purpose (``detect_types`` off): datetimes are
ISO-8601 UTC strings, money amounts are ``Decimal`` serialized as TEXT —
never float.
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from pathlib import Path

logger = structlog.get_logger(__name__)

#: Ordered schema migrations. Entry N (0-based) brings the DB to
#: ``user_version`` N+1. Append-only: NEVER edit or reorder shipped entries.
#: Each entry is a tuple of single statements (not one script) so the whole
#: migration runs inside ONE transaction — ``executescript`` would issue an
#: implicit COMMIT first, breaking atomicity.
MIGRATIONS: tuple[tuple[str, ...], ...] = (
    # v1 — seen_items: cosa abbiamo già visto (e, dallo step 3, notificato)
    (
        """
        CREATE TABLE seen_items (
            item_id       INTEGER PRIMARY KEY,  -- id Vinted dell'articolo
            title         TEXT NOT NULL,
            price         TEXT NOT NULL,        -- Decimal come stringa: mai float
            currency      TEXT NOT NULL,
            brand         TEXT,
            url           TEXT NOT NULL,
            first_seen_at TEXT NOT NULL,        -- UTC, ISO-8601
            notified_at   TEXT                  -- NULL finché non notificato (step 3)
        )
        """,
        "CREATE INDEX idx_seen_items_first_seen_at ON seen_items (first_seen_at)",
    ),
)


def get_connection(db_path: Path) -> sqlite3.Connection:
    """Open (creating if needed) the SQLite DB and return a ready connection.

    On every call: creates the parent directory if missing, opens the file,
    applies any pending migration (transactionally, tracked via
    ``PRAGMA user_version``) and configures the connection:
    WAL journal, foreign keys ON, ``sqlite3.Row`` row factory,
    ``detect_types`` off.

    Re-opening an already migrated file is a safe no-op. The caller owns
    the connection and must ``close()`` it (or use ``contextlib.closing``).

    Raises:
        RuntimeError: if the file's schema version is NEWER than this code
            knows (downgrade attempt — refuse rather than corrupt).
    """
    is_new = not db_path.exists()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(db_path)  # detect_types off by default: conversions are ours
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")

    if is_new:
        logger.info("db_created", path=str(db_path))

    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Apply pending migrations, one transaction each, bumping user_version."""
    current = int(conn.execute("PRAGMA user_version").fetchone()[0])
    target = len(MIGRATIONS)

    if current > target:
        raise RuntimeError(
            f"DB schema version {current} is newer than this code supports ({target}); "
            "refusing to touch it"
        )

    for version in range(current + 1, target + 1):
        statements = MIGRATIONS[version - 1]
        with conn:  # BEGIN … COMMIT (rollback automatico su eccezione)
            for statement in statements:
                conn.execute(statement)
            # user_version non supporta i parametri bind; version è un int nostro.
            conn.execute(f"PRAGMA user_version = {version}")
        logger.info("db_migration_applied", from_version=version - 1, to_version=version)
