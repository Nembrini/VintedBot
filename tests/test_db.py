"""Tests for vintedbot.db: creation, migrations, idempotent reopen, pragmas.

Real files under tmp_path (never :memory:) so directory creation, WAL and
reopening are exercised for real.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from typing import TYPE_CHECKING

import pytest
from structlog.testing import capture_logs

from vintedbot.db import MIGRATIONS, get_connection

if TYPE_CHECKING:
    from pathlib import Path

EXPECTED_COLUMNS = {
    "item_id",
    "title",
    "price",
    "currency",
    "brand",
    "size",        # v2
    "condition",   # v2
    "photo_url",   # v2
    "photo_urls",    # v3
    "published_at",  # v3
    "score",         # v5
    "skipped_at",    # v5
    "url",
    "first_seen_at",
    "notified_at",
}


def _user_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


# ------------------------------------------------------- (a) prima apertura


def test_first_open_creates_file_and_schema(tmp_path: Path) -> None:
    db_path = tmp_path / "vintedbot.db"

    with capture_logs() as logs, closing(get_connection(db_path)) as conn:
        assert db_path.exists()
        assert _user_version(conn) == len(MIGRATIONS)

        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(seen_items)").fetchall()
        }
        assert columns == EXPECTED_COLUMNS

        indexes = {
            row["name"] for row in conn.execute("PRAGMA index_list(seen_items)").fetchall()
        }
        assert "idx_seen_items_first_seen_at" in indexes

    events = [entry["event"] for entry in logs]
    assert "db_created" in events
    assert events.count("db_migration_applied") == len(MIGRATIONS)


# -------------------------------------------------- (b) riapertura: no-op


def test_reopen_is_a_safe_noop(tmp_path: Path) -> None:
    db_path = tmp_path / "vintedbot.db"

    with closing(get_connection(db_path)) as conn:
        with conn:
            conn.execute(
                "INSERT INTO seen_items"
                " (item_id, title, price, currency, url, first_seen_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (1, "giubbotto", "12.50", "EUR", "https://x/items/1", "2026-08-23T10:00:00+00:00"),
            )
        version_before = _user_version(conn)

    with capture_logs() as logs, closing(get_connection(db_path)) as conn:
        # nessuna migrazione ri-applicata, dati intatti
        assert _user_version(conn) == version_before
        row = conn.execute("SELECT * FROM seen_items WHERE item_id = 1").fetchone()
        assert row["title"] == "giubbotto"  # row_factory = sqlite3.Row
        assert row["price"] == "12.50"
        assert row["notified_at"] is None

    events = [entry["event"] for entry in logs]
    assert "db_created" not in events
    assert "db_migration_applied" not in events


# ------------------------------------------- (c) directory padre mancante


def test_missing_parent_directories_are_created(tmp_path: Path) -> None:
    db_path = tmp_path / "deep" / "nested" / "dirs" / "vintedbot.db"
    assert not db_path.parent.exists()

    with closing(get_connection(db_path)) as conn:
        assert db_path.exists()
        assert _user_version(conn) == len(MIGRATIONS)


# --------------------------------------------------------- (d) journal WAL


def test_journal_mode_is_wal(tmp_path: Path) -> None:
    with closing(get_connection(tmp_path / "vintedbot.db")) as conn:
        journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert journal_mode == "wal"
        foreign_keys = conn.execute("PRAGMA foreign_keys").fetchone()[0]
        assert foreign_keys == 1


# ------------------------------ upgrade da v1: nessun dato perso


def test_v1_database_upgrades_without_losing_data(tmp_path: Path) -> None:
    db_path = tmp_path / "vintedbot.db"

    # Costruisci a mano un DB fermo alla v1, con un dato dentro.
    with closing(sqlite3.connect(db_path)) as raw:
        for statement in MIGRATIONS[0]:
            raw.execute(statement)
        raw.execute("PRAGMA user_version = 1")
        raw.execute(
            "INSERT INTO seen_items"
            " (item_id, title, price, currency, url, first_seen_at)"
            " VALUES (1, 'vecchio', '9.99', 'EUR', 'https://x/1', '2026-01-01T00:00:00+00:00')"
        )
        raw.commit()

    with closing(get_connection(db_path)) as conn:
        assert _user_version(conn) == len(MIGRATIONS)  # arrivato all'ultima
        row = conn.execute("SELECT * FROM seen_items WHERE item_id = 1").fetchone()
        assert row["title"] == "vecchio"          # dato sopravvissuto
        assert row["photo_urls"] is None          # colonne v2/v3 presenti, NULL
        assert row["score"] is None and row["skipped_at"] is None  # v5 presenti, NULL
        # tabella v4 creata e funzionante
        assert conn.execute("SELECT COUNT(*) FROM price_observations").fetchone()[0] == 0


# ------------------------------------------------- downgrade: rifiutato


def test_newer_schema_version_is_refused(tmp_path: Path) -> None:
    db_path = tmp_path / "vintedbot.db"
    get_connection(db_path).close()

    with closing(sqlite3.connect(db_path)) as raw:
        raw.execute(f"PRAGMA user_version = {len(MIGRATIONS) + 7}")

    with pytest.raises(RuntimeError, match="newer"):
        get_connection(db_path)
