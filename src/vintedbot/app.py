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

import asyncio
import statistics
from contextlib import closing
from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog

from vintedbot.client import VintedClient
from vintedbot.db import get_connection
from vintedbot.notifier import TelegramError, TelegramNotifier, is_fatal_config_error
from vintedbot.pricing import PriceEstimate, estimate
from vintedbot.repository import ItemRepository, PriceRepository, normalize_brand
from vintedbot.search import search_all

if TYPE_CHECKING:
    from collections.abc import Callable
    from decimal import Decimal

    from vintedbot.config import Settings
    from vintedbot.models import Item, SearchFilters
    from vintedbot.repository import PriceObservation
    from vintedbot.search import PagedSearchResult

logger = structlog.get_logger(__name__)

#: Quanta coda leggere PRIMA di valutare/ordinare/troncare al cap: il
#: taglio anti-valanga deve avvenire DOPO l'ordinamento per punteggio.
_QUEUE_FETCH_LIMIT = 200


class PriceEvaluator:
    """Per-run estimator with one observation read per (brand, catalog)."""

    def __init__(self, price_repo: PriceRepository, settings: Settings) -> None:
        self._repo = price_repo
        self._settings = settings
        self._cache: dict[tuple[str | None, int | None], list[PriceObservation]] = {}

    def estimate_for(self, item: Item, catalog_id: int | None) -> PriceEstimate:
        key = (normalize_brand(item.brand), catalog_id)
        if key not in self._cache:
            self._cache[key] = self._repo.get_observations(
                key[0], catalog_id, self._settings.pricing_max_age_days
            )
        return estimate(
            item.price.amount,
            self._cache[key],
            min_sample_size=self._settings.pricing_min_sample_size,
            max_discount=self._settings.pricing_max_discount,
            confidence_k=self._settings.pricing_confidence_k,
        )


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
    notifications_enabled: bool = False
    notified: int = 0
    notify_failed: int = 0
    notify_queue_remaining: int = 0
    notify_error: str | None = None
    price_observations: int = 0
    price_observations_total: int = 0
    notify_skipped: int = 0
    min_score_active: bool = False


async def run_search(
    settings: Settings,
    filters: SearchFilters,
    *,
    max_pages: int | None,
    max_items: int | None,
    show_all: bool,
    purge_days: int | None,
    notify: bool = True,
    min_score: int | None = None,
    strict_score: bool = False,
    render: Callable[[list[Item], dict[int, PriceEstimate]], None],
) -> SearchOutcome:
    """Run the full search flow and return its outcome.

    Flow: open DB → optional ``purge_older_than`` → paginated search →
    ``filter_new`` (skipped with ``show_all``) → ``render`` the items to
    show (only if any) → ``mark_seen`` on what was rendered (never with
    ``show_all``: consultation mode writes nothing) → notification phase.

    Notification phase (skipped with ``notify=False``, with ``show_all``,
    or — with an explicit warning — when Telegram credentials are missing):
    the queue is ``get_unnotified(cap)``, which naturally contains BOTH the
    items just marked seen AND the backlog of previously failed sends — one
    list, no dual logic. Each successful send is marked notified
    IMMEDIATELY (per item, not batched): if the process dies mid-queue,
    the already-sent ones are never re-notified. A failed send logs a
    warning and the queue continues; a fatal config error (bad token /
    chat not found) aborts the whole queue and surfaces in the outcome.
    Items beyond the anti-flood cap stay unnotified and are drained on
    later runs.

    The DB connection is closed on every exit path. A ``ValueError`` from
    the purge (non-positive days) propagates BEFORE any network work.
    """
    with closing(get_connection(settings.db_path)) as conn:
        repo = ItemRepository(conn)
        price_repo = PriceRepository(conn)

        purged: int | None = None
        if purge_days is not None:
            purged = repo.purge_older_than(purge_days)  # ValueError → al CLI
            # Stessa soglia anche per lo storico prezzi (documentato nel README).
            price_repo.purge_observations_older_than(purge_days)

        async with VintedClient(settings) as client:
            result = await search_all(client, filters, max_pages=max_pages, max_items=max_items)

        # Storico prezzi: TUTTI i risultati, nuovi e già visti — anche con
        # --all (osservare non è notificare). Il catalog è attribuibile solo
        # se la ricerca aveva esattamente una categoria.
        observed_catalog_id = (
            filters.category_ids[0] if len(filters.category_ids) == 1 else None
        )
        observations_written = price_repo.record_observations(
            result.items, observed_catalog_id
        )

        evaluator = PriceEvaluator(price_repo, settings)
        estimates: dict[int, PriceEstimate] = {}
        if show_all:
            shown_items = result.items
            already_seen = 0
        else:
            shown_items = repo.filter_new(result.items)
            already_seen = len(result.items) - len(shown_items)
            estimates = {
                item.id: evaluator.estimate_for(item, observed_catalog_id)
                for item in shown_items
            }

        if shown_items:
            render(shown_items, estimates)

        if shown_items and not show_all:
            # SOLO dopo un rendering riuscito: crash prima del rendering
            # non deve bruciare gli item (doppione > perdita).
            # Lo score salvato è uno snapshot informativo del momento.
            repo.mark_seen(
                shown_items,
                scores={item_id: est.score for item_id, est in estimates.items()},
            )

        notifications_enabled = False
        notified = failed = skipped = queue_remaining = 0
        notify_error: str | None = None
        if notify and not show_all:
            if settings.telegram_bot_token is None or not settings.telegram_chat_id:
                logger.warning(
                    "telegram_not_configured",
                    hint="notifiche saltate: imposta TELEGRAM_BOT_TOKEN e TELEGRAM_CHAT_ID",
                )
            else:
                notifications_enabled = True
                notified, failed, skipped, notify_error = await _notify_queue(
                    settings,
                    repo,
                    evaluator,
                    catalog_id=observed_catalog_id,
                    min_score=min_score,
                    strict_score=strict_score,
                )
                queue_remaining = repo.count_unnotified()
                if queue_remaining:
                    logger.info("notification_queue_remaining", remaining=queue_remaining)

        outcome = SearchOutcome(
            result=result,
            shown_items=shown_items,
            already_seen=already_seen,
            tracked_total=repo.count(),
            filter_bypassed=show_all,
            purged=purged,
            notifications_enabled=notifications_enabled,
            notified=notified,
            notify_failed=failed,
            notify_queue_remaining=queue_remaining,
            notify_error=notify_error,
            price_observations=observations_written,
            price_observations_total=price_repo.count_observations(),
            notify_skipped=skipped,
            min_score_active=min_score is not None,
        )

    logger.info(
        "search_run_done",
        found=len(result.items),
        shown=len(shown_items),
        already_seen=already_seen,
        tracked_total=outcome.tracked_total,
        filter_bypassed=show_all,
        purged=purged,
        notified=outcome.notified,
        notify_failed=outcome.notify_failed,
    )
    return outcome


@dataclass(frozen=True, slots=True)
class BrandSnapshot:
    """Post-backfill picture of one brand within the searched catalog."""

    brand: str
    sample_size: int  # dedup + finestra temporale
    median: Decimal | None


@dataclass(frozen=True, slots=True)
class BackfillOutcome:
    """What one backfill run produced."""

    found: int
    pages_fetched: int
    observations_written: int
    observations_total: int
    catalog_id: int | None
    brands: list[BrandSnapshot]


async def run_backfill(
    settings: Settings,
    filters: SearchFilters,
    *,
    max_pages: int | None,
    max_items: int | None,
) -> BackfillOutcome:
    """Populate the price history WITHOUT touching seen_items or notifying.

    Solves the cold start of the estimator: same search pipeline (and the
    same rate limiting), but the only side effect is
    ``record_observations``. Price caps must be stripped by the caller —
    history needs the expensive listings too.
    """
    with closing(get_connection(settings.db_path)) as conn:
        price_repo = PriceRepository(conn)

        async with VintedClient(settings) as client:
            result = await search_all(client, filters, max_pages=max_pages, max_items=max_items)

        catalog_id = filters.category_ids[0] if len(filters.category_ids) == 1 else None
        written = price_repo.record_observations(result.items, catalog_id)

        brands = sorted(
            {
                normalized
                for item in result.items
                if (normalized := normalize_brand(item.brand)) is not None
            }
        )
        snapshots = []
        for brand in brands:
            observations = price_repo.get_observations(
                brand, catalog_id, settings.pricing_max_age_days
            )
            median = (
                statistics.median(obs.price for obs in observations) if observations else None
            )
            snapshots.append(
                BrandSnapshot(brand=brand, sample_size=len(observations), median=median)
            )

        outcome = BackfillOutcome(
            found=len(result.items),
            pages_fetched=result.pages_fetched,
            observations_written=written,
            observations_total=price_repo.count_observations(),
            catalog_id=catalog_id,
            brands=snapshots,
        )

    logger.info(
        "backfill_done",
        found=outcome.found,
        written=outcome.observations_written,
        total=outcome.observations_total,
        catalog_id=catalog_id,
        brands=len(snapshots),
    )
    return outcome


async def _notify_queue(
    settings: Settings,
    repo: ItemRepository,
    evaluator: PriceEvaluator,
    *,
    catalog_id: int | None,
    min_score: int | None,
    strict_score: bool,
) -> tuple[int, int, int, str | None]:
    """Drain the notification queue; returns (sent, failed, skipped, fatal_error).

    Every candidate (fresh AND backlog) is re-evaluated with the CURRENT
    criteria. With ``min_score`` set, items below the threshold are
    DEFINITIVELY skipped (``skipped_at``: they never re-enter the queue);
    score None passes unless ``strict_score``. Survivors are sorted by
    score DESC — when the anti-flood cap truncates, the best deals go
    first — then at most ``max_notifications_per_run`` are sent, pausing
    between sends, each success marked notified immediately.
    """
    candidates = repo.get_unnotified(_QUEUE_FETCH_LIMIT)
    if not candidates:
        return 0, 0, 0, None

    evaluated = [(item, evaluator.estimate_for(item, catalog_id)) for item in candidates]

    skipped_ids: list[int] = []
    sendable: list[tuple[Item, PriceEstimate]] = []
    if min_score is None:
        sendable = evaluated
    else:
        for item, item_estimate in evaluated:
            if item_estimate.score is None:
                if strict_score:
                    skipped_ids.append(item.id)
                else:
                    sendable.append((item, item_estimate))
            elif item_estimate.score < min_score:
                skipped_ids.append(item.id)
            else:
                sendable.append((item, item_estimate))
        if skipped_ids:
            repo.mark_skipped(skipped_ids)
            logger.info(
                "notifications_skipped_below_threshold",
                skipped=len(skipped_ids),
                min_score=min_score,
                strict=strict_score,
            )

    # Gli affari migliori partono per primi: conta quando il cap tronca.
    sendable.sort(
        key=lambda pair: pair[1].score if pair[1].score is not None else -1,
        reverse=True,
    )
    queue = sendable[: settings.max_notifications_per_run]

    sent = failed = 0
    if queue:
        async with TelegramNotifier(settings) as notifier:
            for position, (item, item_estimate) in enumerate(queue):
                if position > 0 and settings.notify_pause_seconds > 0:
                    await asyncio.sleep(settings.notify_pause_seconds)
                try:
                    await notifier.send_item(item, item_estimate)
                except TelegramError as exc:
                    if is_fatal_config_error(exc):
                        logger.error(
                            "notification_queue_aborted",
                            item_id=item.id,
                            error=str(exc),
                            sent=sent,
                            pending=len(queue) - position,
                        )
                        return sent, failed, len(skipped_ids), str(exc)
                    failed += 1
                    logger.warning(
                        "notification_failed_will_retry",
                        item_id=item.id,
                        error=str(exc),
                    )
                    continue
                # SUBITO, per singolo item: se il processo muore a metà coda,
                # i già inviati non vengono rinotificati al giro successivo.
                repo.mark_notified([item.id])
                sent += 1

    return sent, failed, len(skipped_ids), None
