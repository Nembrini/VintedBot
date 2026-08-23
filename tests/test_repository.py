"""Tests for ItemRepository: filtering, idempotent marking, purge, round-trips.

Real SQLite files under tmp_path, opened through db.get_connection — the
same path production takes.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest

from vintedbot.db import get_connection
from vintedbot.models import Item
from vintedbot.repository import ItemRepository

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture()
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = get_connection(tmp_path / "vintedbot.db")
    yield connection
    connection.close()


@pytest.fixture()
def repo(conn: sqlite3.Connection) -> ItemRepository:
    return ItemRepository(conn)


def make_item(item_id: int, price: str = "10.0", brand: str | None = "Nike") -> Item:
    return Item.model_validate(
        {
            "id": item_id,
            "title": f"item {item_id}",
            "price": {"amount": price, "currency_code": "EUR"},
            "brand_title": brand or "",
            "url": f"https://www.vinted.it/items/{item_id}",
            "user": {"id": 1, "login": "seller"},
        }
    )


# ----------------------------------------------------------- (a,b,c) filter_new


def test_filter_new_on_empty_db_returns_all_in_order(repo: ItemRepository) -> None:
    items = [make_item(3), make_item(1), make_item(2)]
    assert [i.id for i in repo.filter_new(items)] == [3, 1, 2]


def test_filter_new_mixed_batch_keeps_only_unseen_in_order(repo: ItemRepository) -> None:
    repo.mark_seen([make_item(2), make_item(4)])
    items = [make_item(5), make_item(2), make_item(1), make_item(4), make_item(3)]
    assert [i.id for i in repo.filter_new(items)] == [5, 1, 3]


def test_filter_new_empty_batch_returns_empty_without_queries(repo: ItemRepository) -> None:
    assert repo.filter_new([]) == []


# --------------------------------------------------------------- (d) chunking


def test_filter_new_batch_larger_than_sqlite_var_limit(repo: ItemRepository) -> None:
    big_batch = [make_item(i) for i in range(1, 1501)]  # > 999 variabili
    repo.mark_seen(big_batch[:700])  # i primi 700 sono già visti

    new_items = repo.filter_new(big_batch)

    assert [i.id for i in new_items] == list(range(701, 1501))
    assert repo.count() == 700


# ----------------------------------------------------- (e) mark_seen idempotente


def test_mark_seen_twice_is_idempotent(repo: ItemRepository) -> None:
    batch = [make_item(1), make_item(2), make_item(3)]

    assert repo.mark_seen(batch) == 3
    assert repo.mark_seen(batch) == 0  # OR IGNORE: niente crash, niente duplicati
    assert repo.count() == 3


def test_mark_seen_empty_batch(repo: ItemRepository) -> None:
    assert repo.mark_seen([]) == 0


# ------------------------------------------------------------ (f) atomicità


def test_mark_seen_is_atomic_on_mid_batch_failure(
    repo: ItemRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    # NB: una violazione di vincolo (es. NOT NULL) NON serve qui — OR IGNORE
    # la assorbe scartando la riga. Per provare l'atomicità serve un errore
    # che interrompa executemany a metà: un valore non bindabile dal driver.
    original = ItemRepository._item_to_row

    def broken_row(item: Item, first_seen_at_iso: str) -> tuple[object, ...]:
        row = original(item, first_seen_at_iso)
        if item.id == 2:
            return (row[0], object(), *row[2:])  # tipo non supportato da sqlite3
        return row

    monkeypatch.setattr(ItemRepository, "_item_to_row", staticmethod(broken_row))
    batch = [make_item(1), make_item(2), make_item(3)]

    with pytest.raises((sqlite3.InterfaceError, sqlite3.ProgrammingError)):
        repo.mark_seen(batch)

    assert repo.count() == 0  # nemmeno l'item valido prima del guasto è entrato


# ----------------------------------------------------------------- (g,h) purge


def _inject_row(conn: sqlite3.Connection, item_id: int, first_seen_at_iso: str) -> None:
    with conn:
        conn.execute(
            "INSERT INTO seen_items"
            " (item_id, title, price, currency, brand, url, first_seen_at)"
            " VALUES (?, 'old', '1.0', 'EUR', NULL, 'https://x', ?)",
            (item_id, first_seen_at_iso),
        )


def test_purge_older_than_deletes_only_beyond_threshold(
    repo: ItemRepository, conn: sqlite3.Connection
) -> None:
    now = datetime.now(tz=UTC)
    _inject_row(conn, 1, (now - timedelta(days=40)).isoformat())
    _inject_row(conn, 2, (now - timedelta(days=31)).isoformat())
    _inject_row(conn, 3, (now - timedelta(days=5)).isoformat())
    repo.mark_seen([make_item(4)])  # first_seen_at = adesso

    deleted = repo.purge_older_than(30)

    assert deleted == 2
    remaining = {row["item_id"] for row in conn.execute("SELECT item_id FROM seen_items")}
    assert remaining == {3, 4}


@pytest.mark.parametrize("days", [0, -1, -30])
def test_purge_rejects_non_positive_days(repo: ItemRepository, days: int) -> None:
    repo.mark_seen([make_item(1)])
    with pytest.raises(ValueError, match="positive"):
        repo.purge_older_than(days)
    assert repo.count() == 1  # niente svuotamenti accidentali


# ----------------------------------------------- mark_notified / get_unnotified


def test_mark_notified_never_overwrites_existing_timestamp(
    repo: ItemRepository, conn: sqlite3.Connection
) -> None:
    repo.mark_seen([make_item(1), make_item(2)])

    assert repo.mark_notified([1]) == 1
    first_ts = conn.execute(
        "SELECT notified_at FROM seen_items WHERE item_id = 1"
    ).fetchone()["notified_at"]
    assert first_ts is not None

    # Seconda marcatura: 0 righe aggiornate, timestamp INVARIATO.
    assert repo.mark_notified([1, 2]) == 1  # solo l'item 2 era ancora NULL
    assert (
        conn.execute("SELECT notified_at FROM seen_items WHERE item_id = 1").fetchone()[
            "notified_at"
        ]
        == first_ts
    )


def test_get_unnotified_rebuilds_items_and_respects_limit(repo: ItemRepository) -> None:
    repo.mark_seen([make_item(i, price="12.50") for i in (1, 2, 3)])
    repo.mark_notified([2])

    pending = repo.get_unnotified(limit=10)

    assert {item.id for item in pending} == {1, 3}
    rebuilt = pending[0]
    assert rebuilt.price.amount == Decimal("12.50")
    assert rebuilt.brand == "Nike"
    assert rebuilt.seller is None  # non persistito: va bene così
    assert repo.count_unnotified() == 2
    assert len(repo.get_unnotified(limit=1)) == 1


def test_photo_urls_and_published_at_round_trip(repo: ItemRepository) -> None:
    item = Item.model_validate(
        {
            "id": 7,
            "title": "jeans",
            "price": {"amount": "50.0", "currency_code": "EUR"},
            "url": "https://www.vinted.it/items/7",
            "user": {"id": 1, "login": "s"},
            "photo": {
                "url": "https://img/1.jpeg",
                "high_resolution": {"timestamp": 1787478000},
            },
            "photos": [{"url": "https://img/1.jpeg"}, {"url": "https://img/2.jpeg"}],
        }
    )
    repo.mark_seen([item])

    rebuilt = repo.get_unnotified(limit=10)[0]

    assert rebuilt.photo_urls == ("https://img/1.jpeg", "https://img/2.jpeg")
    assert rebuilt.published_at == item.published_at  # aware, identico


# ------------------------------------------------------- (i) round-trip prezzo


def test_price_decimal_round_trip_is_exact(
    repo: ItemRepository, conn: sqlite3.Connection
) -> None:
    repo.mark_seen([make_item(1, price="12.50")])

    row = conn.execute("SELECT price, currency FROM seen_items WHERE item_id = 1").fetchone()
    assert row["price"] == "12.50"  # stringa esatta, mai float
    assert Decimal(row["price"]) == Decimal("12.50")
    assert row["currency"] == "EUR"
