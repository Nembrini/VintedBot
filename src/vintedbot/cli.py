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
import os
import re
import sqlite3
import statistics
import sys
import time
import uuid
from contextlib import closing
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import IntEnum
from pathlib import Path
from typing import TYPE_CHECKING

import structlog
from pydantic import ValidationError
from rich import box
from rich.console import Console
from rich.syntax import Syntax
from rich.table import Table

from vintedbot import __version__
from vintedbot.app import run_all, run_backfill, run_search
from vintedbot.client import VintedError
from vintedbot.config import Settings, get_settings
from vintedbot.health import HealthReporter, HealthState
from vintedbot.lock import LockBusyError, SingleInstanceLock, probe_lock
from vintedbot.log import LOG_FILENAME, bind_run_context, setup_logging_from_settings
from vintedbot.models import SearchFilters
from vintedbot.notifier import TelegramConfigError, TelegramError, TelegramNotifier
from vintedbot.paths import cloud_sync_marker
from vintedbot.schedule import (
    LAUNCHER_RELATIVE_PATH,
    SchedulerError,
    TaskSpec,
    TaskStatus,
    build_task_xml,
    install_task,
    task_status,
    uninstall_task,
)
from vintedbot.searches import SearchConfigError, load_searches

if TYPE_CHECKING:
    from collections.abc import Sequence

    from vintedbot.app import RunAllOutcome, SearchOutcome
    from vintedbot.models import Item
    from vintedbot.pricing import PriceEstimate

_EPOCH = datetime.min.replace(tzinfo=UTC)  # sort fallback for items without a date
_ITEM_URL_RE = re.compile(r"^(https://[^/]+/items/\d+)")

logger = structlog.get_logger(__name__)


class ExitCode(IntEnum):
    """Process exit codes — the scheduler reads these (see README)."""

    OK = 0  # successo, anche con zero risultati
    ERROR = 1  # errore generico durante l'esecuzione
    CONFIG = 2  # configurazione invalida (searches.toml, credenziali, .env)
    LOCKED = 3  # un'altra istanza è già in esecuzione — NON è un guasto
    TIMEOUT = 4  # watchdog: durata massima superata


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
    parser.add_argument(
        "--verbose", action="store_true",
        help="Mostra i log anche su console (di default solo in sessione interattiva).",
    )
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
    search.add_argument(
        "--min-score", type=int, metavar="N", dest="min_score",
        help="Notifica SOLO gli annunci con punteggio affare >= N (0-100). "
             "Gli annunci senza punteggio passano comunque, salvo --strict-score.",
    )
    search.add_argument(
        "--strict-score", action="store_true", dest="strict_score",
        help="Con --min-score: scarta anche gli annunci senza punteggio.",
    )

    run_all_parser = subparsers.add_parser(
        "run-all",
        help="Esegue tutte le ricerche salvate in searches.toml (modalità di produzione).",
    )
    run_all_parser.add_argument(
        "--searches", metavar="PATH", type=Path,
        help="File delle ricerche salvate (default da config: searches.toml).",
    )
    run_all_parser.add_argument(
        "--only", action="append", metavar="NAME",
        help="Esegue solo le ricerche con questo nome (ripetibile: "
             "--only alfa --only beta). Vale anche per ricerche disabilitate.",
    )
    run_all_parser.add_argument(
        "--dry-run", action="store_true", dest="dry_run",
        help="Mostra tutto senza notificare e senza scrivere su seen_items "
             "(le osservazioni prezzo vengono comunque registrate).",
    )
    run_all_parser.add_argument(
        "--ignore-lock", action="store_true", dest="ignore_lock",
        help="Esegue anche se un'altra istanza è in corso. SOLO PER DEBUG: "
             "due run in parallelo possono duplicare notifiche.",
    )

    backfill = subparsers.add_parser(
        "backfill",
        help="Popola lo storico prezzi (nessuna notifica, seen_items intatto).",
    )
    backfill.add_argument("--keyword", help="Testo di ricerca libero.")
    backfill.add_argument("--catalog", action="append", type=int, metavar="ID")
    backfill.add_argument("--brand", action="append", type=int, metavar="ID")
    backfill.add_argument("--size", action="append", type=int, metavar="ID")
    backfill.add_argument("--condition", action="append", type=int, metavar="ID")
    backfill.add_argument("--min-price", type=_decimal_arg, metavar="EUR")
    backfill.add_argument(
        "--max-price", type=_decimal_arg, metavar="EUR",
        help="IGNORATO con un warning: lo storico ha bisogno anche dei prezzi alti.",
    )
    backfill.add_argument("--max-pages", type=int, metavar="N", default=10)
    backfill.add_argument("--max-items", type=int, metavar="N")

    notify_test = subparsers.add_parser(
        "notify-test",
        help="Invia un messaggio di prova su Telegram per verificare le credenziali.",
    )
    notify_test.add_argument(
        "--with-item", action="store_true", dest="with_item",
        help="Invia una notifica di esempio (foto+didascalia) dal primo item "
             "della fixture di test, senza chiamare Vinted.",
    )

    stats = subparsers.add_parser(
        "stats",
        help="Mostra lo storico prezzi per combinazione (brand, categoria).",
    )
    stats.add_argument(
        "--evaluate", type=_decimal_arg, metavar="PREZZO",
        help="Valuta un prezzo contro una combinazione (richiede --brand).",
    )
    stats.add_argument("--brand", help="Nome brand (es. 'just cavalli').")
    stats.add_argument("--catalog", type=int, metavar="ID", help="ID categoria.")

    migrate = subparsers.add_parser(
        "migrate-data",
        help="Copia il database in un'altra posizione (fuori da OneDrive & co.).",
    )
    migrate.add_argument(
        "--to", type=Path, required=True, metavar="PATH",
        help="Percorso di destinazione (file .db o directory).",
    )

    schedule = subparsers.add_parser(
        "schedule",
        help="Registra (o rimuove) l'esecuzione automatica nel Task Scheduler di Windows.",
    )
    schedule_actions = schedule.add_subparsers(dest="schedule_action", required=True)

    schedule_install = schedule_actions.add_parser(
        "install", help="Registra il task che esegue run-all a intervalli regolari."
    )
    schedule_install.add_argument(
        "--dry-run", action="store_true", dest="dry_run",
        help="Stampa l'XML e il comando che verrebbero eseguiti, senza toccare il sistema.",
    )
    schedule_install.add_argument(
        "--interval", type=int, metavar="MIN", dest="interval",
        help="Minuti tra un'esecuzione e l'altra (default da config).",
    )
    schedule_install.add_argument(
        "--random-delay", type=int, metavar="MIN", dest="random_delay",
        help="Ritardo casuale aggiunto a ogni partenza (default da config).",
    )
    schedule_install.add_argument(
        "--yes", action="store_true", dest="assume_yes",
        help="Non chiedere conferma (per uso non interattivo).",
    )
    schedule_actions.add_parser("uninstall", help="Rimuove il task registrato.")
    schedule_actions.add_parser("status", help="Mostra lo stato del task registrato.")

    subparsers.add_parser(
        "doctor",
        help="Diagnosi: dati, lock, ultima esecuzione, schedulazione, credenziali.",
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


def _render_items_table(
    console: Console, items_to_show: list[Item], estimates: dict[int, PriceEstimate]
) -> None:
    """Render the results table (and nothing else) for the given items."""
    now = datetime.now(tz=UTC)
    items: list[Item] = sorted(
        items_to_show, key=lambda item: item.published_at or _EPOCH, reverse=True
    )

    table = Table(title="Risultati Vinted", show_lines=False)
    table.add_column("Titolo", max_width=32, overflow="ellipsis", no_wrap=True)
    table.add_column("Prezzo", justify="right", style="bold", no_wrap=True)
    table.add_column("Affare", justify="right", no_wrap=True)
    table.add_column("Brand", max_width=14, overflow="ellipsis", no_wrap=True)
    table.add_column("Taglia", no_wrap=True)
    table.add_column("Condizione", max_width=12, overflow="ellipsis", no_wrap=True)
    table.add_column("Pubblicato", justify="right", no_wrap=True)
    table.add_column("URL", style="dim", no_wrap=True)

    for item in items:
        item_estimate = estimates.get(item.id)
        score = item_estimate.score if item_estimate is not None else None
        table.add_row(
            item.title,
            f"{item.price.amount} {item.price.currency}",
            f"{score}/100" if score is not None else "—",
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
            f"{outcome.already_seen} già visti"
        )
        if outcome.min_score_active:
            summary += f" / {outcome.notify_skipped} sotto soglia scartati"
        summary += f" / {total_found} totali trovati"
    if outcome.notifications_enabled:
        summary += (
            f" · {outcome.notified} notifiche inviate / {outcome.notify_failed} fallite"
            " (ritenteranno al prossimo giro)"
        )
        if outcome.notify_queue_remaining:
            summary += f" · {outcome.notify_queue_remaining} in coda per i prossimi giri"
    summary += (
        f" · {outcome.price_observations} osservazioni prezzo registrate"
        f" (storico: {outcome.price_observations_total})"
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

    if args.min_score is not None and not 0 <= args.min_score <= 100:
        err_console.print(
            f"[red]Errore:[/red] --min-score deve essere tra 0 e 100 (ricevuto {args.min_score})."
        )
        return 2
    if args.strict_score and args.min_score is None:
        err_console.print("[red]Errore:[/red] --strict-score richiede --min-score.")
        return 2

    def render(items: list[Item], estimates: dict[int, PriceEstimate]) -> None:
        # Nome risolto a runtime dai global del modulo: i test possono
        # sostituire _render_items_table per simulare un rendering fallito.
        _render_items_table(console, items, estimates)

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
                min_score=args.min_score,
                strict_score=args.strict_score,
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
            message += (
                f" · {outcome.price_observations} osservazioni prezzo registrate"
                f" (storico: {outcome.price_observations_total})"
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


def _cmd_run_all(args: argparse.Namespace, console: Console, err_console: Console) -> int:
    """Run every enabled saved search in sequence (the scheduled command).

    Wrapped in the single-instance lock, watched by the deadline, and
    reported through :class:`HealthReporter`: this is the entry point that
    runs with nobody looking at it.
    """
    settings = get_settings()
    path = args.searches or settings.searches_path

    try:
        searches = load_searches(path)
    except SearchConfigError as exc:
        err_console.print(f"[red]Configurazione ricerche non valida:[/red]\n{exc}")
        return ExitCode.CONFIG

    if args.only:
        requested = set(args.only)
        unknown = sorted(requested - {search.name for search in searches})
        if unknown:
            available = ", ".join(repr(search.name) for search in searches)
            names = ", ".join(repr(name) for name in unknown)
            err_console.print(
                f"[red]Errore:[/red] nessuna ricerca chiamata {names}. "
                f"Disponibili: {available}"
            )
            return ExitCode.CONFIG
        # --only è esplicito: esegue anche le ricerche disabilitate.
        # Si mantiene l'ordine del file, non quello degli argomenti.
        searches = [
            search.model_copy(update={"enabled": True})
            for search in searches
            if search.name in requested
        ]

    def render(name: str, items: list[Item], estimates: dict[int, PriceEstimate]) -> None:
        console.print(f"\n[bold cyan]▶ {name}[/bold cyan]")
        _render_items_table(console, items, estimates)

    run_id = uuid.uuid4().hex[:8]
    trigger = invocation_trigger()
    bind_run_context(run_id=run_id, trigger=trigger)
    enabled_names = [search.name for search in searches if search.enabled]

    lock: SingleInstanceLock | None = None
    if args.ignore_lock:
        logger.warning("lock_ignored_debug_only", run_id=run_id)
        console.print("[yellow]--ignore-lock: lock disattivato (solo per debug).[/yellow]")
    else:
        try:
            lock = SingleInstanceLock(settings.lock_path)
            lock.__enter__()
        except LockBusyError as exc:
            # Esito NORMALE quando il giro precedente è ancora in corso:
            # info, nessuna notifica, exit code dedicato.
            logger.info("run_skipped_already_running", holder=exc.holder, run_id=run_id)
            console.print(
                "[yellow]Un'altra istanza è già in esecuzione[/yellow] "
                f"(pid {exc.holder.get('pid', '?')}, "
                f"dalle {exc.holder.get('started_at', '?')}). "
                "Usa --ignore-lock solo per debug."
            )
            return ExitCode.LOCKED
        except OSError as exc:
            err_console.print(f"[red]Impossibile acquisire il lock:[/red] {exc}")
            return ExitCode.ERROR

    logger.info(
        "run_started",
        run_id=run_id,
        searches=enabled_names,
        dry_run=args.dry_run,
        deadline_seconds=settings.max_run_seconds,
        db_path=str(settings.db_path),
    )
    start = time.perf_counter()
    failure: BaseException | None = None
    outcome = None
    try:
        outcome = asyncio.run(
            run_all(
                settings,
                searches,
                dry_run=args.dry_run,
                deadline_seconds=settings.max_run_seconds,
                render=render,
            )
        )
    except (Exception, KeyboardInterrupt) as exc:  # noqa: BLE001 — top-level handler
        failure = exc
    finally:
        if lock is not None:
            lock.__exit__(
                type(failure) if failure is not None else None,
                failure,
                failure.__traceback__ if failure is not None else None,
            )
    duration = time.perf_counter() - start

    return _finish_run(
        outcome,
        failure,
        settings,
        console,
        err_console,
        run_id=run_id,
        duration=duration,
        dry_run=args.dry_run,
        trigger=trigger,
    )


def _finish_run(  # noqa: PLR0913 — è il punto in cui tutti i fili si annodano
    outcome: RunAllOutcome | None,
    failure: BaseException | None,
    settings: Settings,
    console: Console,
    err_console: Console,
    *,
    run_id: str,
    duration: float,
    dry_run: bool,
    trigger: str,
) -> int:
    """Log the closing line, report health, and pick the exit code."""
    reporter = HealthReporter(settings)
    totals = _run_totals(outcome) if outcome is not None else None

    def finish(code: ExitCode, label: str) -> int:
        """Last thing before exiting: `doctor` reads this, not the log file."""
        reporter.record_last_run(
            outcome=label,
            exit_code=int(code),
            duration_seconds=duration,
            trigger=trigger,
            totals=totals,
        )
        return int(code)

    if failure is not None or outcome is None:
        error = failure or RuntimeError("esecuzione terminata senza risultato")
        asyncio.run(reporter.report_failure(error, context="run-all"))
        logger.info(
            "run_finished", run_id=run_id, duration_seconds=round(duration, 1), outcome="crash"
        )
        err_console.print(
            f"[red]Esecuzione fallita:[/red] {type(error).__name__} — "
            f"dettagli nel log ({settings.log_dir / 'vintedbot.log'})."
        )
        return finish(ExitCode.ERROR, "crash")

    console.print(_run_all_summary(outcome, duration, dry_run=dry_run))
    assert totals is not None  # noqa: S101 - outcome non e' None in questo ramo
    logger.info(
        "run_finished",
        run_id=run_id,
        duration_seconds=round(duration, 1),
        searches_executed=len(outcome.reports),
        searches_failed=outcome.failed_count,
        searches_skipped=outcome.skipped_searches,
        new_items=totals["new"],
        notified=totals["notified"],
        skipped_below_threshold=totals["skipped"],
        timed_out=outcome.timed_out,
        aborted=outcome.aborted_reason is not None,
        outcome="ok" if outcome.ok else "error",
    )

    if outcome.timed_out:
        err_console.print(
            f"[red]Watchdog:[/red] superato il tempo massimo "
            f"({settings.max_run_seconds:.0f}s). Ricerche non eseguite: "
            f"{', '.join(outcome.skipped_searches) or 'nessuna'}."
        )
        asyncio.run(
            reporter.report_failure(
                TimeoutError(f"run-all oltre {settings.max_run_seconds:.0f}s"),
                context="watchdog",
            )
        )
        return finish(ExitCode.TIMEOUT, "timeout")

    if outcome.aborted_reason is not None:
        err_console.print(
            f"[red]Esecuzione interrotta:[/red] {outcome.aborted_reason} — "
            "le ricerche rimanenti non sono state eseguite."
        )
        # Il guasto È Telegram: report_failure lo logga senza notificare.
        asyncio.run(
            reporter.report_failure(TelegramError(outcome.aborted_reason), context="notifiche")
        )
        return finish(ExitCode.ERROR, "interrotto")

    if outcome.failed_count:
        asyncio.run(
            reporter.report_failure(
                RuntimeError(f"{outcome.failed_count} ricerche fallite"), context="run-all"
            )
        )
        return finish(ExitCode.ERROR, "errore")

    asyncio.run(reporter.report_success())
    return finish(ExitCode.OK, "ok")


def _run_totals(outcome: RunAllOutcome) -> dict[str, int]:
    totals = {"new": 0, "skipped": 0, "notified": 0}
    for report in outcome.reports:
        if report.outcome is None:
            continue
        totals["new"] += len(report.outcome.shown_items)
        totals["skipped"] += report.outcome.notify_skipped
        totals["notified"] += report.outcome.notified
    return totals


def _run_all_summary(outcome: RunAllOutcome, duration: float, *, dry_run: bool) -> Table:
    """One row per saved search plus the totals of the whole execution."""
    title = "Riepilogo run-all" + (" (dry-run: nessuna notifica)" if dry_run else "")
    table = Table(title=title)
    table.add_column("Ricerca")
    table.add_column("Nuovi", justify="right")
    table.add_column("Scartati", justify="right")
    table.add_column("Notificati", justify="right")
    table.add_column("Esito")

    totals = {"new": 0, "skipped": 0, "notified": 0}
    for report in outcome.reports:
        if report.outcome is None:
            table.add_row(report.name, "—", "—", "—", f"[red]errore:[/red] {report.error}")
            continue
        result = report.outcome
        totals["new"] += len(result.shown_items)
        totals["skipped"] += result.notify_skipped
        totals["notified"] += result.notified
        table.add_row(
            report.name,
            str(len(result.shown_items)),
            str(result.notify_skipped),
            str(result.notified),
            "[green]ok[/green]",
        )

    verdict = (
        f"[red]{outcome.failed_count} fallite[/red]"
        if outcome.failed_count
        else "[green]ok[/green]"
    )
    table.add_section()
    table.add_row(
        f"[bold]TOTALE ({len(outcome.reports)} ricerche · {duration:.1f}s)[/bold]",
        f"[bold]{totals['new']}[/bold]",
        f"[bold]{totals['skipped']}[/bold]",
        f"[bold]{totals['notified']}[/bold]",
        verdict,
    )
    return table


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


def _cmd_backfill(args: argparse.Namespace, console: Console, err_console: Console) -> int:
    """Populate the price history: observations only, no seen/notify effects."""
    if args.max_price is not None:
        err_console.print(
            "[yellow]Avviso:[/yellow] --max-price è IGNORATO nel backfill: per lo "
            "storico servono anche i prezzi alti."
        )
    try:
        filters = SearchFilters(
            keyword=args.keyword,
            category_ids=tuple(args.catalog or ()),
            brand_ids=tuple(args.brand or ()),
            size_ids=tuple(args.size or ()),
            condition_ids=tuple(args.condition or ()),
            price_min=args.min_price,
            price_max=None,  # deliberato: mai un tetto prezzo nello storico
        )
    except ValidationError as exc:
        err_console.print(f"[red]Filtri non validi:[/red] {exc}")
        return 2

    start = time.perf_counter()
    try:
        outcome = asyncio.run(
            run_backfill(
                get_settings(), filters, max_pages=args.max_pages, max_items=args.max_items
            )
        )
    except VintedError as exc:
        err_console.print(f"[red]Errore:[/red] {exc}")
        return 1
    duration = time.perf_counter() - start

    if outcome.brands:
        table = Table(title="Storico dopo il backfill (finestra corrente)")
        table.add_column("Brand")
        table.add_column("Campione", justify="right")
        table.add_column("Mediana", justify="right")
        for snapshot in outcome.brands:
            table.add_row(
                snapshot.brand,
                str(snapshot.sample_size),
                f"{snapshot.median:.2f}" if snapshot.median is not None else "—",
            )
        console.print(table)

    catalog_label = outcome.catalog_id if outcome.catalog_id is not None else "—"
    console.print(
        f"[bold]{outcome.observations_written}[/bold] osservazioni nuove su "
        f"{outcome.found} item · categoria {catalog_label} · "
        f"{outcome.pages_fetched} pagine · {duration:.1f}s — storico totale: "
        f"{outcome.observations_total}"
    )
    return 0


def _cmd_stats(args: argparse.Namespace, console: Console, err_console: Console) -> int:
    """Per-(brand, catalog) history table, or --evaluate for one price."""
    from vintedbot.db import get_connection
    from vintedbot.pricing import estimate
    from vintedbot.repository import PriceRepository

    settings = get_settings()

    if args.evaluate is not None and not args.brand:
        err_console.print("[red]Errore:[/red] --evaluate richiede --brand.")
        return 2

    with closing(get_connection(settings.db_path)) as conn:
        price_repo = PriceRepository(conn)

        if args.evaluate is not None:
            observations = price_repo.get_observations(
                args.brand, args.catalog, settings.pricing_max_age_days
            )
            result = estimate(
                args.evaluate,
                observations,
                min_sample_size=settings.pricing_min_sample_size,
                max_discount=settings.pricing_max_discount,
                confidence_k=settings.pricing_confidence_k,
            )
            catalog_label = args.catalog if args.catalog is not None else "—"
            console.print(
                f"[bold]Valutazione[/bold] {args.evaluate} EUR — brand "
                f"{args.brand!r} · categoria {catalog_label}"
            )
            console.print(f"  campione (dedup, {settings.pricing_max_age_days}g): "
                          f"{result.sample_size}")
            console.print(f"  mediana:  {result.median if result.median is not None else 'n/d'}")
            if result.score is not None and result.discount_pct is not None:
                console.print(f"  sconto:   {result.discount_pct * 100:+.1f}% vs mediana")
                console.print(f"  punteggio: [bold]{result.score}/100[/bold]")
            else:
                console.print(
                    "  punteggio: n/d (campione sotto "
                    f"{settings.pricing_min_sample_size})"
                )
            return 0

        rows = price_repo.stats()
        total = price_repo.count_observations()

        if not rows:
            console.print("Storico prezzi vuoto: nessuna osservazione registrata finora.")
            return 0

        table = Table(title="Storico osservazioni prezzo")
        table.add_column("Brand")
        table.add_column("Categoria", justify="right")
        table.add_column("Osservazioni", justify="right")
        table.add_column("Campione*", justify="right")
        table.add_column("Mediana", justify="right")
        table.add_column("Prima", justify="right")
        table.add_column("Ultima", justify="right")
        for row in rows:
            observations = price_repo.get_observations(
                row.brand, row.catalog_id, settings.pricing_max_age_days
            )
            median = (
                statistics.median(obs.price for obs in observations) if observations else None
            )
            table.add_row(
                row.brand or "—",
                str(row.catalog_id) if row.catalog_id is not None else "—",
                str(row.observations),
                str(len(observations)),
                f"{median:.2f}" if median is not None else "—",
                row.first_observed_at[:16].replace("T", " "),
                row.last_observed_at[:16].replace("T", " "),
            )
        console.print(table)
        console.print(
            f"[bold]{total}[/bold] osservazioni totali · {len(rows)} combinazioni · "
            f"*Campione = dedup per item, finestra {settings.pricing_max_age_days} giorni"
        )
    return 0


_COUNTED_TABLES = ("seen_items", "price_observations")


def _table_counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])  # noqa: S608
        for table in _COUNTED_TABLES
    }


def _cmd_migrate_data(args: argparse.Namespace, console: Console, err_console: Console) -> int:
    """Copy the database to a new location, WAL contents included.

    Uses ``Connection.backup()`` rather than copying files: with WAL
    enabled a plain file copy of the ``.db`` silently drops whatever is
    still in the ``-wal`` sidecar. The source is left untouched — verify
    the counts, update ``.env``, then delete it yourself.
    """
    from contextlib import closing

    from vintedbot.db import get_connection

    settings = get_settings()
    source = settings.db_path
    destination: Path = args.to
    if destination.is_dir() or not destination.suffix:
        destination = destination / source.name

    if not source.exists():
        err_console.print(f"[red]Errore:[/red] database di origine non trovato: {source}")
        return ExitCode.CONFIG
    if destination.exists():
        err_console.print(
            f"[red]Errore:[/red] la destinazione esiste già: {destination}\n"
            "Rimuovila o scegli un altro percorso: non sovrascrivo un DB esistente."
        )
        return ExitCode.CONFIG

    destination.parent.mkdir(parents=True, exist_ok=True)
    with closing(get_connection(source)) as source_conn:
        before = _table_counts(source_conn)
        with closing(sqlite3.connect(destination)) as target_conn:
            source_conn.backup(target_conn)

    with closing(get_connection(destination)) as check_conn:
        after = _table_counts(check_conn)

    console.print(f"Origine:      {source}")
    console.print(f"Destinazione: {destination}")
    for table in _COUNTED_TABLES:
        status = "[green]ok[/green]" if before[table] == after[table] else "[red]DIVERSO[/red]"
        console.print(f"  {table}: {before[table]} → {after[table]} {status}")

    if before != after:
        err_console.print(
            "[red]Verifica fallita:[/red] i conteggi non coincidono, "
            "la destinazione NON è affidabile."
        )
        return ExitCode.ERROR

    console.print(
        "\n[green]Copia verificata.[/green] Aggiungi al tuo .env:\n"
        f"  VINTEDBOT_DB_PATH={destination}\n"
        f"  VINTEDBOT_DATA_DIR={destination.parent}\n"
        "L'originale è rimasto al suo posto: cancellalo tu a verifica fatta."
    )
    return ExitCode.OK


def invocation_trigger() -> str:
    """How this execution was started: ``scheduler`` or ``manual``.

    The launcher exports ``VINTEDBOT_INVOKED_BY``; preferring an
    environment variable to a CLI flag keeps the task's command line
    identical to the one you would type by hand, so the two are
    reproducible from each other.
    """
    source = os.environ.get("VINTEDBOT_INVOKED_BY", "").strip().lower()
    return source or "manual"


def project_dir() -> Path:
    """Root of the checkout: where ``.env``, ``searches.toml`` and ``scripts/`` live."""
    candidate = Path(__file__).resolve().parents[2]
    if (candidate / LAUNCHER_RELATIVE_PATH).exists():
        return candidate
    return Path.cwd().resolve()


#: Windows shows the task's exit code as "Ultimo risultato esecuzione".
#: Ours are 0-4; the 0x413xx ones are the scheduler's own.
_TASK_RESULT_MEANING = {
    0: "ok",
    1: "errore durante l'esecuzione",
    2: "configurazione invalida",
    3: "già in esecuzione — non è un guasto",
    4: "watchdog: durata massima superata",
    0x41300: "task pronto, mai eseguito",
    0x41301: "esecuzione in corso",
    0x41303: "mai eseguito",
    0x41306: "esecuzione terminata dall'utente",
}


def _schedule_preview(spec: TaskSpec, settings: Settings) -> Table:
    """Human-readable summary of what would be registered."""
    table = Table(title="Task da registrare", show_header=False, box=box.SIMPLE)
    table.add_column("Voce", style="bold")
    table.add_column("Valore")
    jitter = (
        f" (+ fino a {spec.random_delay_minutes} min casuali)"
        if spec.random_delay_minutes
        else ""
    )
    table.add_row("Nome", spec.task_name)
    table.add_row("Esegue", str(spec.command))
    table.add_row("Cartella di lavoro", str(spec.working_directory))
    table.add_row("Frequenza", f"ogni {spec.interval_minutes} minuti{jitter}, 24 ore su 24")
    table.add_row("Utente", f"{spec.user_id} — S4U: nessuna password salvata")
    table.add_row(
        "Limite di durata",
        f"{spec.execution_time_limit_seconds / 60:.0f} min "
        f"(watchdog interno: {settings.max_run_seconds / 60:.0f} min)",
    )
    table.add_row("Se già in corso", "la nuova esecuzione non parte")
    table.add_row("A batteria", "parte e prosegue comunque")
    table.add_row("Dopo spegnimento", "recupera l'esecuzione persa, senza svegliare il PC")
    table.add_row("Privilegi", "utente normale (nessun amministratore)")
    table.add_row("Finestra", "nessuna: gira in sessione 0")
    return table


def _schedule_install(
    args: argparse.Namespace, settings: Settings, console: Console, err_console: Console
) -> int:
    """Show exactly what would be registered, then register it — never silently."""
    overrides: dict[str, object] = {}
    if args.interval is not None:
        overrides["scheduler_interval_minutes"] = args.interval
    if args.random_delay is not None:
        overrides["scheduler_random_delay_minutes"] = args.random_delay
    if overrides:
        try:
            # Rivalidato dal modello: i limiti stanno in config.py, non qui.
            settings = Settings.model_validate({**settings.model_dump(), **overrides})
        except ValidationError as exc:
            err_console.print(f"[red]Parametri non validi:[/red]\n{exc}")
            return ExitCode.CONFIG

    spec = TaskSpec.from_settings(
        settings, project_dir=project_dir(), now=datetime.now(tz=UTC).astimezone()
    )
    if not spec.command.exists():
        err_console.print(
            f"[red]Errore:[/red] launcher non trovato: {spec.command}\n"
            "Senza di esso il task non avrebbe niente da eseguire."
        )
        return ExitCode.CONFIG

    xml = build_task_xml(spec)
    console.print(_schedule_preview(spec, settings))
    console.print("[dim]Definizione XML che verrebbe registrata:[/dim]")
    console.print(Syntax(xml, "xml", theme="ansi_dark", background_color="default"))

    if args.dry_run:
        console.print(
            "\n[yellow]--dry-run: niente è stato registrato.[/yellow] "
            "Ripeti senza --dry-run per procedere."
        )
        return ExitCode.OK

    if not args.assume_yes:
        if not sys.stdin.isatty():
            err_console.print(
                "[red]Serve una conferma[/red] ma la sessione non è interattiva: "
                "riesegui con --yes se sei sicuro."
            )
            return ExitCode.CONFIG
        if input("Registrare questo task? [s/N] ").strip().lower() not in {"s", "si", "sì", "y"}:
            console.print("Annullato: niente è stato registrato.")
            return ExitCode.OK

    install_task(spec)
    console.print(
        f"\n[green]Task registrato:[/green] {spec.task_name}\n"
        f"Verifica con: vintedbot schedule status — oppure vintedbot doctor\n"
        f"Rimuovi con:  vintedbot schedule uninstall"
    )
    return ExitCode.OK


def _schedule_uninstall(settings: Settings, console: Console) -> int:
    """Idempotent: nothing registered is a normal outcome, not an error."""
    if uninstall_task(settings.scheduler_task_name):
        console.print(f"[green]Task rimosso:[/green] {settings.scheduler_task_name}")
    else:
        console.print(
            f"Nessun task da rimuovere: {settings.scheduler_task_name} non è registrato."
        )
    return ExitCode.OK


def _schedule_status(settings: Settings, console: Console) -> int:
    status = task_status(settings.scheduler_task_name)
    console.print(_task_status_table(settings, status))
    return ExitCode.OK


def _task_status_table(settings: Settings, status: TaskStatus) -> Table:
    table = Table(title="Schedulazione", show_header=False, box=box.SIMPLE)
    table.add_column("Voce", style="bold")
    table.add_column("Valore")
    table.add_row("Nome", settings.scheduler_task_name)
    if not status.registered:
        table.add_row("Stato", "[yellow]non registrato[/yellow]")
        table.add_row("", "Registralo con: vintedbot schedule install")
        return table
    table.add_row("Stato", status.state or "?")
    table.add_row("Ultima esecuzione", status.last_run_time or "mai")
    table.add_row("Ultimo risultato", _format_task_result(status))
    table.add_row("Prossima esecuzione", status.next_run_time or "—")
    return table


def _format_task_result(status: TaskStatus) -> str:
    if status.last_result is None:
        return "—"
    meaning = _TASK_RESULT_MEANING.get(status.last_result, "codice sconosciuto")
    colour = "green" if status.last_result == 0 else "yellow"
    return f"[{colour}]{status.last_result_hex}[/{colour}] — {meaning}"


def _cmd_schedule(args: argparse.Namespace, console: Console, err_console: Console) -> int:
    """Install, remove or inspect the Windows scheduled task."""
    settings = get_settings()
    try:
        if args.schedule_action == "install":
            return _schedule_install(args, settings, console, err_console)
        if args.schedule_action == "uninstall":
            return _schedule_uninstall(settings, console)
        return _schedule_status(settings, console)
    except SchedulerError as exc:
        err_console.print(f"[red]Task Scheduler:[/red] {exc}")
        return ExitCode.ERROR


def _format_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":  # noqa: PLR2004
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"  # pragma: no cover - irraggiungibile, il loop chiude prima


def _doctor_database(settings: Settings) -> tuple[str, str]:
    """Describe the database. Never migrates it, never creates it."""
    path = settings.db_path
    if not path.exists():
        return (f"{path}\nassente: verrà creato alla prima esecuzione", "attenzione")
    size = _format_size(path.stat().st_size)
    try:
        # sqlite3.connect diretto, NON db.get_connection: `doctor` deve
        # fotografare lo stato, non applicare migrazioni di nascosto.
        with closing(sqlite3.connect(path)) as conn:
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            counts = {
                table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])  # noqa: S608
                for table in _COUNTED_TABLES
            }
    except sqlite3.Error as exc:
        return (f"{path}\n[red]illeggibile o danneggiato[/red]: {exc}", "errore")
    detail = " · ".join(f"{table} {count}" for table, count in counts.items())
    return (f"{path}\n{size} · schema v{version} · {detail}", "ok")


def _doctor_logs(settings: Settings) -> tuple[str, str]:
    log_file = settings.log_dir / LOG_FILENAME
    if not log_file.exists():
        return (f"{log_file}\nnessun log ancora scritto", "attenzione")
    archived = len(list(settings.log_dir.glob(f"{LOG_FILENAME}.*")))
    size = _format_size(log_file.stat().st_size)
    suffix = f" (+{archived} archiviati)" if archived else ""
    return (f"{log_file}\n{size}{suffix}", "ok")


def _doctor_lock(settings: Settings) -> tuple[str, str]:
    status = probe_lock(settings.lock_path)
    if not status.held:
        return ("libero", "ok")
    pid = status.holder.get("pid", "?")
    since = status.holder.get("started_at", "?")
    return (f"tenuto dal pid {pid}, dalle {since}", "ok")


def _doctor_last_run(settings: Settings) -> tuple[str, str]:
    """Read the last run from health.json — authoritative, unlike the log text."""
    state = HealthState.load(settings.health_path)
    if not state.last_run:
        return ("nessuna esecuzione registrata", "attenzione")
    last = state.last_run
    finished = str(last.get("finished_at", "?"))
    try:
        when = datetime.fromisoformat(finished).astimezone()
        stamp = f"{when:%Y-%m-%d %H:%M} ({_relative_time(when, datetime.now(tz=UTC))})"
    except ValueError:
        stamp = finished
    outcome = str(last.get("outcome", "?"))
    parts = [
        f"esito {outcome}",
        f"exit {last.get('exit_code', '?')}",
        f"{last.get('duration_seconds', '?')}s",
        f"avvio {last.get('trigger', '?')}",
    ]
    if "notified" in last:
        parts.append(f"{last['notified']} notificati")
    status = "ok" if outcome == "ok" else "attenzione"
    details = " · ".join(parts)
    return (f"{stamp}\n{details}", status)


def _doctor_searches(settings: Settings) -> tuple[str, str]:
    try:
        searches = load_searches(settings.searches_path)
    except SearchConfigError as exc:
        return (f"[red]{exc}[/red]", "errore")
    enabled = [search.name for search in searches if search.enabled]
    if not enabled:
        return (f"{settings.searches_path}\nnessuna ricerca attiva", "attenzione")
    return (f"{settings.searches_path}\n{len(enabled)} attive: {', '.join(enabled)}", "ok")


def _doctor_credentials(settings: Settings) -> tuple[str, str]:
    """Presence only — the values themselves must never reach the screen."""
    missing = []
    if settings.telegram_bot_token is None:
        missing.append("token")
    if not settings.telegram_chat_id:
        missing.append("chat id")
    if missing:
        return (f"mancano: {', '.join(missing)} — le notifiche non partiranno", "attenzione")
    return ("token e chat id configurati", "ok")


def _doctor_schedule(settings: Settings) -> tuple[str, str]:
    try:
        status = task_status(settings.scheduler_task_name)
    except SchedulerError as exc:
        return (f"{settings.scheduler_task_name}\nnon interrogabile: {exc}", "attenzione")
    if not status.registered:
        return (
            f"{settings.scheduler_task_name}\nnon registrato "
            "— attivalo con: vintedbot schedule install",
            "attenzione",
        )
    detail = (
        f"stato {status.state} · ultima {status.last_run_time or 'mai'} "
        f"· risultato {_format_task_result(status)} "
        f"· prossima {status.next_run_time or '—'}"
    )
    return (f"{settings.scheduler_task_name}\n{detail}", "ok")


_DOCTOR_MARK = {
    "ok": "[green]ok[/green]",
    "attenzione": "[yellow]attenzione[/yellow]",
    "errore": "[red]errore[/red]",
}


def _cmd_doctor(args: argparse.Namespace, console: Console, err_console: Console) -> int:
    """One screen answering "is the bot alive?" — and never crashing while doing it."""
    del args, err_console
    settings = get_settings()

    checks: list[tuple[str, tuple[str, str]]] = [
        ("Cartella dati", (str(settings.data_dir), "ok")),
        ("Database", _doctor_database(settings)),
        ("Log", _doctor_logs(settings)),
        ("Lock", _doctor_lock(settings)),
        ("Ultima esecuzione", _doctor_last_run(settings)),
        ("Schedulazione", _doctor_schedule(settings)),
        ("Ricerche salvate", _doctor_searches(settings)),
        ("Credenziali Telegram", _doctor_credentials(settings)),
    ]

    table = Table(title="VintedBot — diagnosi", box=box.SIMPLE)
    table.add_column("Controllo", style="bold", no_wrap=True)
    table.add_column("Valore")
    table.add_column("Esito", no_wrap=True)
    for label, (value, status) in checks:
        table.add_row(label, value, _DOCTOR_MARK[status])
    console.print(table)

    marker = cloud_sync_marker(settings.db_path)
    if marker is not None:
        console.print(f"[yellow]I dati sono in una cartella sincronizzata ({marker}).[/yellow]")

    # Solo un guasto vero (database illeggibile, ricerche invalide) merita
    # un exit code diverso da zero: gli "attenzione" sono cose da fare, non rotture.
    broken = [label for label, (_, status) in checks if status == "errore"]
    if broken:
        console.print(f"[red]Da sistemare:[/red] {', '.join(broken)}")
        return ExitCode.ERROR
    return ExitCode.OK


def _warn_if_cloud_synced(settings: Settings, err_console: Console) -> None:
    """Nag (once per run) when the database lives in a synced folder."""
    marker = cloud_sync_marker(settings.db_path)
    if marker is None:
        return
    logger.warning(
        "db_in_cloud_synced_folder",
        db_path=str(settings.db_path),
        service=marker,
        hint="usa `vintedbot migrate-data --to <data dir>`",
    )
    err_console.print(
        f"[yellow]Attenzione:[/yellow] il database è dentro una cartella sincronizzata "
        f"({marker}): {settings.db_path}\n"
        "La sincronizzazione può bloccare il file e corrompere il WAL. "
        "Spostalo con: vintedbot migrate-data --to <percorso>"
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for the ``vintedbot`` script and ``python -m vintedbot``."""
    args = _build_parser().parse_args(argv)
    settings = get_settings()
    setup_logging_from_settings(settings, verbose=getattr(args, "verbose", False))

    console = Console()
    err_console = Console(stderr=True)
    _warn_if_cloud_synced(settings, err_console)

    if args.command == "search":
        return _cmd_search(args, console, err_console)
    if args.command == "notify-test":
        return _cmd_notify_test(args, console, err_console)
    if args.command == "run-all":
        return _cmd_run_all(args, console, err_console)
    if args.command == "backfill":
        return _cmd_backfill(args, console, err_console)
    if args.command == "stats":
        return _cmd_stats(args, console, err_console)
    if args.command == "migrate-data":
        return _cmd_migrate_data(args, console, err_console)
    if args.command == "schedule":
        return _cmd_schedule(args, console, err_console)
    if args.command == "doctor":
        return _cmd_doctor(args, console, err_console)
    raise AssertionError("unreachable: argparse enforces a valid subcommand")


if __name__ == "__main__":
    sys.exit(main())
