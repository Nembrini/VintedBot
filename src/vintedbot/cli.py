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
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import ValidationError
from rich.console import Console
from rich.table import Table

from vintedbot import __version__
from vintedbot.app import run_search
from vintedbot.client import VintedError
from vintedbot.config import get_settings
from vintedbot.log import setup_logging
from vintedbot.models import SearchFilters
from vintedbot.notifier import TelegramConfigError, TelegramError, TelegramNotifier

if TYPE_CHECKING:
    from collections.abc import Sequence

    from vintedbot.app import SearchOutcome
    from vintedbot.models import Item

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
    search.add_argument(
        "--all", action="store_true", dest="show_all",
        help="Mostra TUTTI i risultati (bypassa il filtro 'già visti') senza scrivere nel DB.",
    )
    search.add_argument(
        "--purge-days", type=int, metavar="N",
        help="Prima della ricerca elimina dal DB i record visti da più di N giorni.",
    )
    search.add_argument(
        "--no-notify", action="store_true", dest="no_notify",
        help="Salta le notifiche Telegram (solo tabella, come lo step 2).",
    )

    notify_test = subparsers.add_parser(
        "notify-test",
        help="Invia un messaggio di prova su Telegram per verificare le credenziali.",
    )
    notify_test.add_argument(
        "--with-item", action="store_true", dest="with_item",
        help="Invia una notifica di esempio (foto+didascalia) dal primo item "
             "della fixture di test, senza chiamare Vinted.",
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


def _render_items_table(console: Console, items_to_show: list[Item]) -> None:
    """Render the results table (and nothing else) for the given items."""
    now = datetime.now(tz=UTC)
    items: list[Item] = sorted(
        items_to_show, key=lambda item: item.published_at or _EPOCH, reverse=True
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


def _summary_line(outcome: SearchOutcome, duration_seconds: float) -> str:
    """Build the final summary: nuovi / già visti / totali — stato DB."""
    total_found = len(outcome.result.items)
    if outcome.filter_bypassed:
        summary = (
            f"[bold]{total_found}[/bold] totali trovati "
            "([yellow]filtro disattivato: --all, nessuna scrittura[/yellow])"
        )
    else:
        summary = (
            f"[bold]{len(outcome.shown_items)}[/bold] nuovi / "
            f"{outcome.already_seen} già visti / {total_found} totali trovati"
        )
    if outcome.notifications_enabled:
        summary += (
            f" · {outcome.notified} notifiche inviate / {outcome.notify_failed} fallite"
            " (ritenteranno al prossimo giro)"
        )
        if outcome.notify_queue_remaining:
            summary += f" · {outcome.notify_queue_remaining} in coda per i prossimi giri"
    summary += (
        f" · {outcome.result.pages_fetched} pagine · {duration_seconds:.1f}s"
        f" — DB: {outcome.tracked_total} item tracciati"
    )
    if outcome.purged is not None:
        summary += f" ({outcome.purged} eliminati dal purge)"
    if outcome.result.partial_failure:
        summary += " · [yellow]risultato parziale: una pagina è fallita (vedi log)[/yellow]"
    return summary


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

    def render(items: list[Item]) -> None:
        # Nome risolto a runtime dai global del modulo: i test possono
        # sostituire _render_items_table per simulare un rendering fallito.
        _render_items_table(console, items)

    start = time.perf_counter()
    try:
        outcome = asyncio.run(
            run_search(
                get_settings(),
                filters,
                max_pages=args.max_pages,
                max_items=args.max_items,
                show_all=args.show_all,
                purge_days=args.purge_days,
                notify=not args.no_notify,
                render=render,
            )
        )
    except ValueError as exc:  # --purge-days non valido (dal repository)
        err_console.print(f"[red]Errore:[/red] --purge-days non valido: {exc}")
        return 2
    except VintedError as exc:
        err_console.print(f"[red]Errore:[/red] {exc}")
        return 1
    duration = time.perf_counter() - start

    if not outcome.shown_items:
        # Esito normale della maggior parte delle esecuzioni schedulate.
        if outcome.filter_bypassed:
            console.print("Nessun risultato trovato.")
        else:
            message = (
                f"Nessun nuovo annuncio ({outcome.already_seen} già visti)"
                f" — DB: {outcome.tracked_total} item tracciati"
            )
            if outcome.notified or outcome.notify_failed:
                # Anche senza nuovi, la coda arretrata può aver lavorato.
                message += (
                    f" · {outcome.notified} notifiche arretrate inviate"
                    f" / {outcome.notify_failed} fallite"
                )
            console.print(message)
    else:
        console.print(_summary_line(outcome, duration))

    if outcome.notify_error:
        err_console.print(
            f"[red]Notifiche interrotte:[/red] {outcome.notify_error} — "
            "gli item non inviati ritenteranno al prossimo giro."
        )
        return 1
    return 0  # zero risultati non è un errore


_FIXTURE_PATH = Path("tests/fixtures/catalog_items_page1.json")


def _load_fixture_item() -> Item:
    """First item of the test fixture, parsed with the real models."""
    import json

    from vintedbot.models import parse_items

    raw = json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))
    items = parse_items(raw["items"])
    if not items:
        raise ValueError("la fixture non contiene item parsabili")
    return items[0]


async def _send_test_message(item: Item | None) -> None:
    async with TelegramNotifier() as notifier:
        if item is not None:
            await notifier.send_item(item)
        else:
            await notifier.send_text("VintedBot: notifiche configurate correttamente ✅")


def _cmd_notify_test(args: argparse.Namespace, console: Console, err_console: Console) -> int:
    item: Item | None = None
    if args.with_item:
        if not _FIXTURE_PATH.exists():
            err_console.print(
                f"[red]Fixture non trovata:[/red] {_FIXTURE_PATH} — lancia il comando "
                "dalla radice del progetto."
            )
            return 2
        item = _load_fixture_item()

    try:
        asyncio.run(_send_test_message(item))
    except TelegramConfigError as exc:
        err_console.print(f"[red]Configurazione mancante:[/red] {exc}")
        return 2
    except TelegramError as exc:
        err_console.print(f"[red]Invio fallito:[/red] {exc}")
        return 1
    console.print("[green]Notifica di test inviata ✅ — controlla Telegram.[/green]")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for the ``vintedbot`` script and ``python -m vintedbot``."""
    args = _build_parser().parse_args(argv)
    settings = get_settings()
    setup_logging(settings.log_level, json_output=settings.log_json)

    console = Console()
    err_console = Console(stderr=True)

    if args.command == "search":
        return _cmd_search(args, console, err_console)
    if args.command == "notify-test":
        return _cmd_notify_test(args, console, err_console)
    raise AssertionError("unreachable: argparse enforces a valid subcommand")


if __name__ == "__main__":
    sys.exit(main())
