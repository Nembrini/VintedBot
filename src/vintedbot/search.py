"""High-level search service: paginate a saved search into a clean item list.

Sits above :class:`~vintedbot.client.VintedClient`. Pages are fetched
strictly IN SEQUENCE — the rate limit matters more than speed at this
stage — deduplicated by item id (with ``newest_first`` results can slide
between pages as new listings arrive mid-iteration) and capped by both a
page and an item budget.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog

from vintedbot.client import VintedError
from vintedbot.config import get_settings

if TYPE_CHECKING:
    from vintedbot.client import VintedClient
    from vintedbot.models import Item, SearchFilters

logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class PagedSearchResult:
    """Outcome of a paginated search.

    Attributes:
        items: deduplicated items, in the order the API returned them.
        pages_fetched: pages successfully fetched.
        partial_failure: True when a page failed after the client's retries
            and we returned what was collected up to that point.
    """

    items: list[Item]
    pages_fetched: int
    partial_failure: bool


async def search_all(
    client: VintedClient,
    filters: SearchFilters,
    *,
    max_pages: int | None = None,
    max_items: int | None = None,
) -> PagedSearchResult:
    """Fetch up to ``max_pages`` pages of a search, sequentially.

    Stops on the first empty page, on the page budget, or once ``max_items``
    items have been collected (defaults for both come from config).

    Failure policy — partial results beat total loss: if a page fails after
    the client's own retries, a warning is logged and the items collected so
    far are returned (``partial_failure=True``). A failure on the *first*
    page is a total loss, so it propagates as-is.
    """
    if max_pages is None or max_items is None:
        settings = get_settings()
        max_pages = max_pages if max_pages is not None else settings.search_max_pages
        max_items = max_items if max_items is not None else settings.search_max_items

    seen_ids: set[int] = set()
    items: list[Item] = []
    pages_fetched = 0
    partial_failure = False

    for page in range(1, max_pages + 1):
        try:
            page_items = await client.search(filters, page)
        except VintedError as exc:
            if not items:
                raise  # nothing collected: a partial result would hide a real outage
            logger.warning(
                "page_failed_returning_partial",
                page=page,
                error=str(exc),
                error_type=exc.__class__.__name__,
                collected=len(items),
            )
            partial_failure = True
            break

        pages_fetched += 1
        if not page_items:
            logger.debug("pagination_stop_empty_page", page=page)
            break

        new_items = [item for item in page_items if item.id not in seen_ids]
        seen_ids.update(item.id for item in new_items)
        items.extend(new_items)
        logger.debug(
            "page_collected",
            page=page,
            new=len(new_items),
            duplicates=len(page_items) - len(new_items),
            total=len(items),
        )

        if len(items) >= max_items:
            del items[max_items:]
            logger.debug("pagination_stop_max_items", page=page, max_items=max_items)
            break

    return PagedSearchResult(
        items=items, pages_fetched=pages_fetched, partial_failure=partial_failure
    )
