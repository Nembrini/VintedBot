"""Application use-cases orchestrating client, search and persistence.

``cli.py`` stays a thin layer (argument parsing + presentation); this
module owns the "run a search" flow: open DB, optional purge, fetch,
filter already-seen items, render via callback, then mark as seen.

Rendering happens BEFORE ``mark_seen`` on purpose: if the program crashes
before the user saw the items, they must NOT be recorded as seen — they
would be lost forever. Accepted trade-off: a crash between rendering and
``mark_seen`` can show a duplicate on the next run. Duplicate beats loss.
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog

from vintedbot.client import VintedClient
from vintedbot.db import get_connection
from vintedbot.repository import ItemRepository
from vintedbot.search import search_all

if TYPE_CHECKING:
    from collections.abc import Callable

    from vintedbot.config import Settings
    from vintedbot.models import Item, SearchFilters
    from vintedbot.search import PagedSearchResult

logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class SearchOutcome:
    """What one run of the search use-case produced (for the CLI summary).

    Attributes:
        result: the raw paginated search result (all items found).
        shown_items: the items handed to the renderer (new ones, or all
            with ``show_all``).
        already_seen: items filtered out because already in ``seen_items``
            (always 0 with ``show_all``: the filter never ran).
        tracked_total: ``seen_items`` row count at the end of the run.
        filter_bypassed: True when ``show_all`` disabled filter and writes.
        purged: rows deleted by the pre-search purge; None if not requested.
    """

    result: PagedSearchResult
    shown_items: list[Item]
    already_seen: int
    tracked_total: int
    filter_bypassed: bool
    purged: int | None


async def run_search(
    settings: Settings,
    filters: SearchFilters,
    *,
    max_pages: int | None,
    max_items: int | None,
    show_all: bool,
    purge_days: int | None,
    render: Callable[[list[Item]], None],
) -> SearchOutcome:
    """Run the full search flow and return its outcome.

    Flow: open DB → optional ``purge_older_than`` → paginated search →
    ``filter_new`` (skipped with ``show_all``) → ``render`` the items to
    show (only if any) → ``mark_seen`` on what was rendered (never with
    ``show_all``: consultation mode writes nothing).

    The DB connection is closed on every exit path. A ``ValueError`` from
    the purge (non-positive days) propagates BEFORE any network work.
    """
    with closing(get_connection(settings.db_path)) as conn:
        repo = ItemRepository(conn)

        purged: int | None = None
        if purge_days is not None:
            purged = repo.purge_older_than(purge_days)  # ValueError → al CLI

        async with VintedClient(settings) as client:
            result = await search_all(client, filters, max_pages=max_pages, max_items=max_items)

        if show_all:
            shown_items = result.items
            already_seen = 0
        else:
            shown_items = repo.filter_new(result.items)
            already_seen = len(result.items) - len(shown_items)

        if shown_items:
            render(shown_items)

        if shown_items and not show_all:
            # SOLO dopo un rendering riuscito: crash prima del rendering
            # non deve bruciare gli item (doppione > perdita).
            repo.mark_seen(shown_items)

        outcome = SearchOutcome(
            result=result,
            shown_items=shown_items,
            already_seen=already_seen,
            tracked_total=repo.count(),
            filter_bypassed=show_all,
            purged=purged,
        )

    logger.info(
        "search_run_done",
        found=len(result.items),
        shown=len(shown_items),
        already_seen=already_seen,
        tracked_total=outcome.tracked_total,
        filter_bypassed=show_all,
        purged=purged,
    )
    return outcome
