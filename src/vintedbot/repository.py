"""Data access for the ``seen_items`` table — the ONLY module containing SQL.

The rest of the codebase talks to :class:`ItemRepository` in terms of
:class:`~vintedbot.models.Item` objects; every conversion to/from storage
representation (Decimal → TEXT, aware datetime → ISO-8601 UTC string)
happens here, in one place.

The repository receives an already-open connection from
:func:`vintedbot.db.get_connection` (dependency injection: it never opens
the DB itself, so tests can hand it a temporary file).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    import sqlite3

    from vintedbot.models import Item

logger = structlog.get_logger(__name__)

#: Chunk size for ``WHERE item_id IN (…)`` placeholder lists. Older SQLite
#: builds cap host parameters at 999 (SQLITE_MAX_VARIABLE_NUMBER); we stay
#: comfortably below regardless of the build we run on.
_MAX_SQL_VARS = 900


class ItemRepository:
    """All queries on ``seen_items``.

    Args:
        conn: an open, configured connection (see ``db.get_connection``).
            The repository does not own it: closing it is the caller's job.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    # ------------------------------------------------------------- queries

    def filter_new(self, items: list[Item]) -> list[Item]:
        """Return only the items whose id has never been seen, input order kept.

        One ``SELECT … WHERE item_id IN (…)`` per chunk of at most
        :data:`_MAX_SQL_VARS` ids (parametrized placeholders, never string
        interpolation), then an in-memory set difference — no per-item
        round-trips. An empty batch returns an empty list with zero queries.
        """
        if not items:
            return []

        ids = [item.id for item in items]
        seen_ids: set[int] = set()
        for start in range(0, len(ids), _MAX_SQL_VARS):
            chunk = ids[start : start + _MAX_SQL_VARS]
            placeholders = ",".join("?" * len(chunk))
            rows = self._conn.execute(
                f"SELECT item_id FROM seen_items WHERE item_id IN ({placeholders})",  # noqa: S608 — placeholders only
                chunk,
            ).fetchall()
            seen_ids.update(row["item_id"] for row in rows)

        new_items = [item for item in items if item.id not in seen_ids]
        logger.debug(
            "filter_new_done",
            batch=len(items),
            new=len(new_items),
            already_seen=len(items) - len(new_items),
        )
        return new_items

    def mark_seen(self, items: list[Item]) -> int:
        """Insert the batch into ``seen_items``; returns rows actually inserted.

        ``INSERT OR IGNORE`` + ``executemany`` in ONE explicit transaction:
        either the whole batch lands or nothing does, and re-marking already
        seen items neither crashes nor duplicates (idempotent). Note that
        OR IGNORE skips ANY constraint-violating row (duplicate id, NULL in
        a NOT NULL column) rather than aborting; only driver/database errors
        abort — and then roll back — the whole batch.

        ``first_seen_at`` is set to now (UTC, ISO-8601) for the whole batch;
        ``notified_at`` stays NULL until step 3.

        Counting note: we measure ``conn.total_changes`` before/after instead
        of ``cursor.rowcount``. With ``executemany`` the DB-API leaves
        ``rowcount`` semantics loose and sqlite3 has historically been
        inconsistent across versions, while ``total_changes`` counts real
        modifications only — rows skipped by OR IGNORE don't inflate it.
        """
        if not items:
            return 0

        now_iso = datetime.now(tz=UTC).isoformat()
        rows = [self._item_to_row(item, now_iso) for item in items]

        changes_before = self._conn.total_changes
        with self._conn:  # BEGIN … COMMIT; rollback automatico su eccezione
            self._conn.executemany(
                "INSERT OR IGNORE INTO seen_items"
                " (item_id, title, price, currency, brand, url, first_seen_at, notified_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, NULL)",
                rows,
            )
        inserted = self._conn.total_changes - changes_before

        logger.info("items_marked_seen", batch=len(items), inserted=inserted)
        return inserted

    def purge_older_than(self, days: int) -> int:
        """Delete records first seen more than ``days`` days ago (UTC).

        Uses the ``idx_seen_items_first_seen_at`` index: all writes go
        through :meth:`mark_seen`, so ``first_seen_at`` has one uniform
        ISO-8601 UTC format and lexicographic comparison equals temporal
        comparison.

        Raises:
            ValueError: if ``days <= 0`` — protection against accidentally
                emptying the table.
        """
        if days <= 0:
            raise ValueError(f"days must be positive, got {days}")

        cutoff_iso = (datetime.now(tz=UTC) - timedelta(days=days)).isoformat()
        with self._conn:
            cursor = self._conn.execute(
                "DELETE FROM seen_items WHERE first_seen_at < ?", (cutoff_iso,)
            )
        deleted = cursor.rowcount  # affidabile per una singola execute

        logger.info("items_purged", days=days, deleted=deleted)
        return deleted

    def count(self) -> int:
        """Total number of records in ``seen_items``."""
        row = self._conn.execute("SELECT COUNT(*) AS n FROM seen_items").fetchone()
        return int(row["n"])

    # ------------------------------------------------------------ internals

    @staticmethod
    def _item_to_row(
        item: Item, first_seen_at_iso: str
    ) -> tuple[int, str, str, str, str | None, str, str]:
        """Single conversion point Item → DB row (Decimal as string, never float)."""
        return (
            item.id,
            item.title,
            str(item.price.amount),
            item.price.currency,
            item.brand,
            item.url,
            first_seen_at_iso,
        )
