"""Command-line interface for VintedBot.

argparse (not typer): one subcommand with flat options does not justify an
extra dependency and decorator magic; ``rich`` is the only new runtime
dependency, needed for the output table anyway.

All console output lives in this module — lower layers (client, search)
only emit structured logs.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
import time
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING

from pydantic import ValidationError
from rich.console import Console
from rich.table import Table

from vintedbot import __version__
from vintedbot.client import VintedClient, VintedError
from vintedbot.config import get_settings
from vintedbot.log import setup_logging
from vintedbot.models import SearchFilters
from vintedbot.search import search_all

if TYPE_CHECKING:
    from collections.abc import Sequence

    from vintedbot.models import Item
    from vintedbot.search import PagedSearchResult

_EPOCH = datetime.min.replace(tzinfo=UTC)  # sort fallback for items without a date
_ITEM_URL_RE = re.compile(r"^(https://[^/]+/items/\d+)")


def _short_url(url: str) -> str:
    """Strip the optional SEO slug: https://…/items/123-titolo-lungo → …/items/123."""
    match = _ITEM_URL_RE.match(url)
    return match.group(1) if match else url


def _decimal_arg(value: str) -> Decimal:
    try:
        return Decimal(value)
    except InvalidOperation:
        raise argparse.ArgumentTypeError(f"prezzo non valido: {value!r}") from None


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vintedbot",
        description="Monitora annunci Vinted secondo filtri salvati.",
    )
    parser.add_argument("--version", action="version", version=f"vintedbot {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    search = subparsers.add_parser("search", help="Esegue una ricerca sul catalogo Vinted.")
    search.add_argument("--keyword", help="Testo di ricerca libero.")
    search.add_argument(
        "--catalog", action="append", type=int, metavar="ID",
        help="ID categoria Vinted (ripetibile; vedi docs/api_notes.md per scoprirli).",
    )
    search.add_argument(
        "--brand", action="append", type=int, metavar="ID", help="ID brand (ripetibile)."
    )
    search.add_argument(
        "--size", action="append", type=int, metavar="ID", help="ID taglia (ripetibile)."
    )
    search.add_argument(
        "--condition", action="append", type=int, metavar="ID",
        help="ID condizione (ripetibile).",
    )
    search.add_argument("--min-price", type=_decimal_arg, metavar="EUR", help="Prezzo minimo.")
    search.add_argument("--max-price", type=_decimal_arg, metavar="EUR", help="Prezzo massimo.")
    search.add_argument(
        "--max-pages", type=int, metavar="N",
        help="Massimo pagine da scaricare (default da config).",
    )
    search.add_argument(
        "--max-items", type=int, metavar="N",
        help="Massimo articoli da raccogliere (default da config).",
    )
    return parser


def _relative_time(published_at: datetime | None, now: datetime) -> str:
    """Human-friendly Italian relative time: 'adesso', '5 min fa', '3 h fa', '2 g fa'."""
    if published_at is None:
        return "?"
    seconds = max(0.0, (now - published_at).total_seconds())
    if seconds < 60:
        return "adesso"
    if seconds < 3600:
        return f"{int(seconds // 60)} min fa"
    if seconds < 86400:
        return f"{int(seconds // 3600)} h fa"
    return f"{int(seconds // 86400)} g fa"


def _render_table(console: Console, result: PagedSearchResult, duration_seconds: float) -> None:
    now = datetime.now(tz=UTC)
    items: list[Item] = sorted(
        result.items, key=lambda item: item.published_at or _EPOCH, reverse=True
    )

    table = Table(title="Risultati Vinted", show_lines=False)
    table.add_column("Titolo", max_width=32, overflow="ellipsis", no_wrap=True)
    table.add_column("Prezzo", justify="right", style="bold", no_wrap=True)
    table.add_column("Brand", max_width=14, overflow="ellipsis", no_wrap=True)
    table.add_column("Taglia", no_wrap=True)
    table.add_column("Condizione", max_width=12, overflow="ellipsis", no_wrap=True)
    table.add_column("Pubblicato", justify="right", no_wrap=True)
    table.add_column("URL", style="dim", no_wrap=True)

    for item in items:
        table.add_row(
            item.title,
            f"{item.price.amount} {item.price.currency}",
            item.brand or "—",
            item.size or "—",
            item.condition or "—",
            _relative_time(item.published_at, now),
            _short_url(item.url),
        )

    console.print(table)
    summary = (
        f"[bold]{len(items)}[/bold] risultati · "
        f"{result.pages_fetched} pagine · {duration_seconds:.1f}s"
    )
    if result.partial_failure:
        summary += " · [yellow]risultato parziale: una pagina è fallita (vedi log)[/yellow]"
    console.print(summary)


async def _run_search(
    filters: SearchFilters, max_pages: int | None, max_items: int | None
) -> PagedSearchResult:
    async with VintedClient() as client:
        return await search_all(client, filters, max_pages=max_pages, max_items=max_items)


def _cmd_search(args: argparse.Namespace, console: Console, err_console: Console) -> int:
    try:
        filters = SearchFilters(
            keyword=args.keyword,
            category_ids=tuple(args.catalog or ()),
            brand_ids=tuple(args.brand or ()),
            size_ids=tuple(args.size or ()),
            condition_ids=tuple(args.condition or ()),
            price_min=args.min_price,
            price_max=args.max_price,
        )
    except ValidationError as exc:
        err_console.print(f"[red]Filtri non validi:[/red] {exc}")
        return 2

    start = time.perf_counter()
    try:
        result = asyncio.run(_run_search(filters, args.max_pages, args.max_items))
    except VintedError as exc:
        err_console.print(f"[red]Errore:[/red] {exc}")
        return 1
    duration = time.perf_counter() - start

    _render_table(console, result, duration)
    return 0  # zero risultati non è un errore


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for the ``vintedbot`` script and ``python -m vintedbot``."""
    args = _build_parser().parse_args(argv)
    settings = get_settings()
    setup_logging(settings.log_level, json_output=settings.log_json)

    console = Console()
    err_console = Console(stderr=True)

    if args.command == "search":
        return _cmd_search(args, console, err_console)
    raise AssertionError("unreachable: argparse enforces a valid subcommand")


if __name__ == "__main__":
    sys.exit(main())
