"""Tests for vintedbot.models: parsing (real fixture), tolerance, filter serialization."""

from __future__ import annotations

import copy
from datetime import datetime
from decimal import Decimal
from typing import Any

import pytest
from structlog.testing import capture_logs

from vintedbot.models import Item, SearchFilters, SortOrder, parse_items

# ------------------------------------------------------------- (a) parsing


def test_real_fixture_parses_completely(catalog_page: dict[str, Any]) -> None:
    raw_items = catalog_page["items"]
    items = parse_items(raw_items)

    assert len(items) == len(raw_items) > 0
    for item in items:
        assert isinstance(item, Item)
        assert isinstance(item.price.amount, Decimal)
        assert item.price.currency == "EUR"
        assert item.url.startswith("https://")
        assert item.seller.id > 0
        assert item.seller.username
        assert item.photo_url is not None
        assert isinstance(item.published_at, datetime)
        assert item.published_at.tzinfo is not None  # aware, mai naive


# ------------------------------------------- (b) tolleranza item malformati


def test_malformed_item_is_discarded_and_logged(catalog_page: dict[str, Any]) -> None:
    raw_items = copy.deepcopy(catalog_page["items"])
    del raw_items[1]["price"]  # rompi un item a metà pagina
    del raw_items[1]["title"]

    with capture_logs() as logs:
        items = parse_items(raw_items)

    assert len(items) == len(raw_items) - 1
    discarded = [entry for entry in logs if entry["event"] == "item_discarded"]
    assert len(discarded) == 1
    assert discarded[0]["item_id"] == raw_items[1]["id"]


def test_blank_strings_normalized_to_none() -> None:
    raw = {
        "id": 1,
        "title": "x",
        "price": {"amount": "5.0", "currency_code": "EUR"},
        "brand_title": "",
        "url": "https://www.vinted.it/items/1-x",
        "user": {"id": 2, "login": "someone"},
    }
    item = Item.model_validate(raw)
    assert item.brand is None
    assert item.size is None
    assert item.photo_url is None  # nessuna foto: niente crash
    assert item.published_at is None


# --------------------------------------------- (c) serializzazione filtri


def test_filters_serialize_to_exact_api_params() -> None:
    """Must match the request captured in docs/api_notes.md §1."""
    filters = SearchFilters(
        category_ids=(2536,), size_ids=(208,), price_max=Decimal("20")
    )
    assert filters.to_query_params() == {
        "order": "newest_first",
        "currency": "EUR",
        "catalog_ids": "2536",
        "size_ids": "208",
        "price_to": "20",
    }


def test_filters_full_serialization() -> None:
    filters = SearchFilters(
        keyword="giubbotto",
        category_ids=(2536, 79),
        brand_ids=(14,),
        size_ids=(208,),
        condition_ids=(2, 3),
        price_min=Decimal("5"),
        price_max=Decimal("20"),
        order=SortOrder.PRICE_LOW_TO_HIGH,
    )
    assert filters.to_query_params() == {
        "order": "price_low_to_high",
        "currency": "EUR",
        "search_text": "giubbotto",
        "catalog_ids": "2536,79",
        "brand_ids": "14",
        "size_ids": "208",
        "status_ids": "2,3",
        "price_from": "5",
        "price_to": "20",
    }


def test_filters_price_range_validated() -> None:
    with pytest.raises(ValueError, match="price_min"):
        SearchFilters(price_min=Decimal("30"), price_max=Decimal("20"))
