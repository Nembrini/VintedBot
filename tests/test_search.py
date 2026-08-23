"""Tests for vintedbot.search.search_all: pagination, dedup, partial failure."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest
from structlog.testing import capture_logs

from vintedbot.client import VintedError
from vintedbot.models import Item
from vintedbot.search import search_all

if TYPE_CHECKING:
    from vintedbot.client import VintedClient
    from vintedbot.models import SearchFilters


def make_item(item_id: int) -> Item:
    return Item.model_validate(
        {
            "id": item_id,
            "title": f"item {item_id}",
            "price": {"amount": "10.0", "currency_code": "EUR"},
            "url": f"https://www.vinted.it/items/{item_id}",
            "user": {"id": 1, "login": "seller"},
        }
    )


class StubClient:
    """Duck-typed stand-in for VintedClient: scripted pages, no network."""

    def __init__(self, pages: dict[int, list[Item] | Exception]) -> None:
        self._pages = pages
        self.calls: list[int] = []

    async def search(
        self, filters: SearchFilters, page: int = 1, *, per_page: int = 96
    ) -> list[Item]:
        self.calls.append(page)
        outcome = self._pages.get(page, [])
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def as_client(stub: StubClient) -> VintedClient:
    return cast("VintedClient", stub)


FILTERS = cast("SearchFilters", None)  # search_all only forwards it to the client


# ------------------------------------------------------------ (d) paginazione


async def test_stops_on_empty_page() -> None:
    stub = StubClient({1: [make_item(1), make_item(2)], 2: []})
    result = await search_all(as_client(stub), FILTERS, max_pages=10, max_items=100)
    assert [item.id for item in result.items] == [1, 2]
    assert stub.calls == [1, 2]
    assert result.pages_fetched == 2
    assert result.partial_failure is False


async def test_respects_max_pages() -> None:
    stub = StubClient({1: [make_item(1)], 2: [make_item(2)], 3: [make_item(3)]})
    result = await search_all(as_client(stub), FILTERS, max_pages=2, max_items=100)
    assert [item.id for item in result.items] == [1, 2]
    assert stub.calls == [1, 2]  # la pagina 3 non viene mai richiesta


async def test_respects_max_items_and_stops_early() -> None:
    stub = StubClient({1: [make_item(1), make_item(2), make_item(3)], 2: [make_item(4)]})
    result = await search_all(as_client(stub), FILTERS, max_pages=10, max_items=2)
    assert [item.id for item in result.items] == [1, 2]
    assert stub.calls == [1]  # budget esaurito: niente pagina 2


async def test_deduplicates_ids_across_pages() -> None:
    # newest_first fa slittare gli item: l'id 2 ricompare in pagina 2
    stub = StubClient(
        {1: [make_item(1), make_item(2)], 2: [make_item(2), make_item(3)], 3: []}
    )
    result = await search_all(as_client(stub), FILTERS, max_pages=10, max_items=100)
    assert [item.id for item in result.items] == [1, 2, 3]


# ------------------------------------------------------- (e) fallimento parziale


async def test_partial_failure_returns_collected_items() -> None:
    stub = StubClient({1: [make_item(1)], 2: VintedError("boom")})
    with capture_logs() as logs:
        result = await search_all(as_client(stub), FILTERS, max_pages=10, max_items=100)

    assert [item.id for item in result.items] == [1]
    assert result.partial_failure is True
    assert result.pages_fetched == 1
    warnings = [entry for entry in logs if entry["event"] == "page_failed_returning_partial"]
    assert len(warnings) == 1 and warnings[0]["page"] == 2


async def test_first_page_failure_propagates() -> None:
    stub = StubClient({1: VintedError("total outage")})
    with pytest.raises(VintedError, match="total outage"):
        await search_all(as_client(stub), FILTERS, max_pages=10, max_items=100)
