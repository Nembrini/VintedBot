"""Orchestration tests for the notification phase of the search flow.

Everything faked: Vinted client on synthetic items, Telegram notifier as a
scriptable stub, DB in tmp_path, sleeps recorded — zero network.
"""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import closing
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest

import vintedbot.app
import vintedbot.cli
from vintedbot.cli import main
from vintedbot.config import Settings
from vintedbot.db import get_connection
from vintedbot.models import Item
from vintedbot.notifier import TelegramError
from vintedbot.pricing import PriceEstimate
from vintedbot.repository import ItemRepository, PriceRepository

if TYPE_CHECKING:
    from pathlib import Path

ARGS = ["search", "--catalog", "2536"]
TOKEN = "123456789:AAfaketokenfaketokenfaketoken"


def make_item(item_id: int, price: str = "10.0") -> Item:
    return Item.model_validate(
        {
            "id": item_id,
            "title": f"item {item_id}",
            "price": {"amount": price, "currency_code": "EUR"},
            "url": f"https://www.vinted.it/items/{item_id}",
            "user": {"id": 1, "login": "seller"},
            "photo": {"url": f"https://images1.vinted.net/{item_id}.jpeg"},
        }
    )


class FakeClient:
    def __init__(self, pages: dict[int, list[Item]]) -> None:
        self._pages = pages

    async def __aenter__(self) -> FakeClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def search(self, filters: object, page: int = 1, *, per_page: int = 96) -> list[Item]:
        return self._pages.get(page, [])


class FakeNotifier:
    """Scriptable TelegramNotifier stub shared across a test via class state."""

    sent_ids: list[int] = []
    sent_estimates: dict[int, object] = {}
    failures: dict[int, Exception] = {}
    instantiated: int = 0

    def __init__(self, settings: object = None, **kwargs: object) -> None:
        FakeNotifier.instantiated += 1

    async def __aenter__(self) -> FakeNotifier:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def send_item(self, item: Item, estimate: object = None) -> None:
        failure = FakeNotifier.failures.get(item.id)
        if failure is not None:
            raise failure
        FakeNotifier.sent_ids.append(item.id)
        FakeNotifier.sent_estimates[item.id] = estimate


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "vintedbot.db"


@pytest.fixture(autouse=True)
def _wire(monkeypatch: pytest.MonkeyPatch, db_path: Path) -> None:
    FakeNotifier.sent_ids = []
    FakeNotifier.sent_estimates = {}
    FakeNotifier.failures = {}
    FakeNotifier.instantiated = 0

    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        db_path=db_path,
        telegram_bot_token=TOKEN,
        telegram_chat_id="42",
        notify_pause_seconds=1.0,
    )
    monkeypatch.setattr(vintedbot.cli, "get_settings", lambda: settings)
    monkeypatch.setattr(vintedbot.cli, "setup_logging", lambda *a, **kw: None)
    monkeypatch.setattr(vintedbot.app, "TelegramNotifier", FakeNotifier)

    async def fake_sleep(delay: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)


def set_pages(monkeypatch: pytest.MonkeyPatch, items: list[Item]) -> None:
    monkeypatch.setattr(
        vintedbot.app, "VintedClient", lambda _settings: FakeClient({1: items})
    )


def notified_map(db_path: Path) -> dict[int, str | None]:
    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        return {
            row["item_id"]: row["notified_at"]
            for row in conn.execute("SELECT item_id, notified_at FROM seen_items")
        }


# --------------------------------------------------------- (a) tutto ok


def test_all_sends_succeed(
    monkeypatch: pytest.MonkeyPatch, db_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    set_pages(monkeypatch, [make_item(1), make_item(2), make_item(3)])

    assert main(ARGS) == 0

    assert sorted(FakeNotifier.sent_ids) == [1, 2, 3]
    assert all(ts is not None for ts in notified_map(db_path).values())
    out = capsys.readouterr().out
    assert "3 notifiche inviate / 0 fallite" in out


# ---------------------------------- (b) fallimento singolo + recupero


def test_single_failure_is_retried_next_run(
    monkeypatch: pytest.MonkeyPatch, db_path: Path
) -> None:
    set_pages(monkeypatch, [make_item(1), make_item(2), make_item(3)])
    FakeNotifier.failures = {2: TelegramError("foto rotta", status_code=400,
                                              description="failed to get http url content")}

    assert main(ARGS) == 0

    state = notified_map(db_path)
    assert state[1] is not None and state[3] is not None
    assert state[2] is None  # fallito: resta in coda

    # Giro successivo: Vinted non dà nulla di nuovo, l'invio ora riesce.
    FakeNotifier.failures = {}
    FakeNotifier.sent_ids = []
    assert main(ARGS) == 0

    assert FakeNotifier.sent_ids == [2]  # SOLO l'arretrato
    assert all(ts is not None for ts in notified_map(db_path).values())


# ------------------------------------------- (c) 401 → coda interrotta


def test_fatal_config_error_aborts_queue(
    monkeypatch: pytest.MonkeyPatch, db_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    items = [make_item(1), make_item(2), make_item(3)]
    set_pages(monkeypatch, items)
    FakeNotifier.failures = {
        item.id: TelegramError("token invalido", status_code=401) for item in items
    }

    rc = main(ARGS)

    assert rc != 0
    assert FakeNotifier.sent_ids == []
    assert all(ts is None for ts in notified_map(db_path).values())
    assert "Notifiche interrotte" in capsys.readouterr().err


# ------------------------------------------------------ (d) --no-notify


def test_no_notify_flag_skips_telegram_entirely(
    monkeypatch: pytest.MonkeyPatch, db_path: Path
) -> None:
    set_pages(monkeypatch, [make_item(1), make_item(2)])

    assert main([*ARGS, "--no-notify"]) == 0

    assert FakeNotifier.instantiated == 0  # mai istanziato
    assert all(ts is None for ts in notified_map(db_path).values())


# -------------------------------------------- (e) credenziali mancanti


def test_missing_credentials_behave_like_no_notify(
    monkeypatch: pytest.MonkeyPatch, db_path: Path
) -> None:
    settings = Settings(_env_file=None, db_path=db_path)  # type: ignore[call-arg]
    monkeypatch.setattr(vintedbot.cli, "get_settings", lambda: settings)
    set_pages(monkeypatch, [make_item(1)])

    from structlog.testing import capture_logs

    with capture_logs() as logs:
        rc = main(ARGS)

    assert rc == 0
    assert FakeNotifier.instantiated == 0
    assert all(ts is None for ts in notified_map(db_path).values())
    assert any(entry["event"] == "telegram_not_configured" for entry in logs)


# ----------------------------------------------------- (f) anti-valanga


def test_notification_cap_and_drain_on_next_run(
    monkeypatch: pytest.MonkeyPatch, db_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    items = [make_item(i) for i in range(1, 16)]  # 15 nuovi, cap = 10
    set_pages(monkeypatch, items)

    assert main(ARGS) == 0

    state = notified_map(db_path)
    assert sum(ts is not None for ts in state.values()) == 10
    assert sum(ts is None for ts in state.values()) == 5
    assert "5 in coda per i prossimi giri" in capsys.readouterr().out

    # Giro successivo: i 5 rimasti vengono smaltiti.
    FakeNotifier.sent_ids = []
    assert main(ARGS) == 0
    assert len(FakeNotifier.sent_ids) == 5
    assert all(ts is not None for ts in notified_map(db_path).values())


# ==================================== filtro affare --min-score (step 4.2)
#
# Punteggi pilotati monkeypatchando vintedbot.app.estimate: il prezzo
# dell'item codifica lo score desiderato ("75.0" → 75; "0.5" → None).


def install_fake_estimate(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_estimate(price: Decimal, observations: object, **kwargs: object) -> PriceEstimate:
        score = None if price < 1 else int(price)
        return PriceEstimate(
            median=Decimal("100"),
            sample_size=50,
            observed_from="2026-06-01T00:00:00+00:00",
            observed_to="2026-08-20T00:00:00+00:00",
            score=score,
            discount_pct=None if score is None else 0.3,
        )

    monkeypatch.setattr(vintedbot.app, "estimate", fake_estimate)


def _skipped_ids(db_path: Path) -> set[int]:
    with closing(sqlite3.connect(db_path)) as conn:
        return {
            row[0]
            for row in conn.execute(
                "SELECT item_id FROM seen_items WHERE skipped_at IS NOT NULL"
            )
        }


# ------------------------------------------------- (e) filtro OFF


def test_filter_off_notifies_everything_with_estimates(
    monkeypatch: pytest.MonkeyPatch, db_path: Path
) -> None:
    install_fake_estimate(monkeypatch)
    set_pages(monkeypatch, [make_item(1, price="75.0"), make_item(2, price="40.0")])

    assert main(ARGS) == 0

    assert sorted(FakeNotifier.sent_ids) == [1, 2]  # nessuno scartato
    sent_estimate = FakeNotifier.sent_estimates[1]
    assert isinstance(sent_estimate, PriceEstimate) and sent_estimate.score == 75
    assert _skipped_ids(db_path) == set()


# ---------------------------------------- (f) filtro ON 60: 75 / 40 / None


def test_threshold_filters_low_scores_none_passes(
    monkeypatch: pytest.MonkeyPatch, db_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    install_fake_estimate(monkeypatch)
    items = [make_item(1, price="75.0"), make_item(2, price="40.0"), make_item(3, price="0.5")]
    set_pages(monkeypatch, items)

    assert main([*ARGS, "--min-score", "60"]) == 0

    assert sorted(FakeNotifier.sent_ids) == [1, 3]  # il 75 e il senza-punteggio
    assert _skipped_ids(db_path) == {2}
    assert "1 sotto soglia scartati" in capsys.readouterr().out


def test_strict_score_also_drops_unscored(
    monkeypatch: pytest.MonkeyPatch, db_path: Path
) -> None:
    install_fake_estimate(monkeypatch)
    items = [make_item(1, price="75.0"), make_item(2, price="40.0"), make_item(3, price="0.5")]
    set_pages(monkeypatch, items)

    assert main([*ARGS, "--min-score", "60", "--strict-score"]) == 0

    assert FakeNotifier.sent_ids == [1]
    assert _skipped_ids(db_path) == {2, 3}


# ------------------------------------------- (g) lo scartato non torna


def test_skipped_item_never_returns(
    monkeypatch: pytest.MonkeyPatch, db_path: Path
) -> None:
    install_fake_estimate(monkeypatch)
    set_pages(monkeypatch, [make_item(2, price="40.0")])
    assert main([*ARGS, "--min-score", "60"]) == 0
    assert _skipped_ids(db_path) == {2}

    FakeNotifier.sent_ids = []
    assert main(ARGS) == 0  # anche SENZA filtro: scartato è definitivo

    assert FakeNotifier.sent_ids == []


# ---------------------------------- (h) arretrato rivalutato al retry


def test_backlog_item_reevaluated_with_current_criteria(
    monkeypatch: pytest.MonkeyPatch, db_path: Path
) -> None:
    install_fake_estimate(monkeypatch)
    set_pages(monkeypatch, [make_item(2, price="40.0")])
    # run 1 senza filtro, invio fallisce → resta in coda (non skipped)
    FakeNotifier.failures = {2: TelegramError("foto rotta", status_code=400,
                                              description="failed to get http url content")}
    assert main(ARGS) == 0
    assert _skipped_ids(db_path) == set()

    # run 2 CON filtro: l'arretrato viene rivalutato e stavolta scartato.
    FakeNotifier.failures = {}
    FakeNotifier.sent_ids = []
    assert main([*ARGS, "--min-score", "60"]) == 0

    assert FakeNotifier.sent_ids == []
    assert _skipped_ids(db_path) == {2}


# --------------------------- (i) ordinamento per score + anti-valanga


def test_queue_sorted_by_score_before_cap(
    monkeypatch: pytest.MonkeyPatch, db_path: Path
) -> None:
    install_fake_estimate(monkeypatch)
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None, db_path=db_path,
        telegram_bot_token=TOKEN, telegram_chat_id="42",
        max_notifications_per_run=2,
    )
    monkeypatch.setattr(vintedbot.cli, "get_settings", lambda: settings)
    set_pages(
        monkeypatch,
        [make_item(1, price="50.0"), make_item(2, price="90.0"), make_item(3, price="70.0")],
    )

    assert main(ARGS) == 0

    assert FakeNotifier.sent_ids == [2, 3]  # 90 poi 70: il 50 resta in coda


# ------------------------------------------ (j) cache per combinazione


def test_one_observation_read_per_brand_catalog(
    monkeypatch: pytest.MonkeyPatch, db_path: Path
) -> None:
    from vintedbot.repository import PriceRepository

    calls: list[tuple[str | None, int | None]] = []
    original = PriceRepository.get_observations

    def spy(
        self: PriceRepository, brand: str | None, catalog_id: int | None, max_age_days: int
    ) -> object:
        calls.append((brand, catalog_id))
        return original(self, brand, catalog_id, max_age_days)

    monkeypatch.setattr(PriceRepository, "get_observations", spy)
    # 10 item, stesso brand (None) e stessa categoria → UNA lettura in tutto,
    # condivisa tra valutazione dei nuovi e coda di notifica.
    set_pages(monkeypatch, [make_item(i) for i in range(1, 11)])

    assert main(ARGS) == 0
    assert len(calls) == 1


# --------------------- (C.1) coerenza storico/purge dopo gli scarti


def test_skipped_item_is_observed_once_per_run(
    monkeypatch: pytest.MonkeyPatch, db_path: Path
) -> None:
    install_fake_estimate(monkeypatch)
    set_pages(monkeypatch, [make_item(1, price="75.0"), make_item(2, price="40.0")])

    assert main([*ARGS, "--min-score", "60"]) == 0
    assert _skipped_ids(db_path) == {2}

    with closing(sqlite3.connect(db_path)) as conn:
        dups = conn.execute(
            "SELECT COUNT(*) FROM (SELECT item_id, observed_at FROM price_observations"
            " GROUP BY item_id, observed_at HAVING COUNT(*) > 1)"
        ).fetchone()[0]
        skipped_rows = conn.execute(
            "SELECT COUNT(*) FROM price_observations WHERE item_id = 2"
        ).fetchone()[0]
    assert dups == 0
    assert skipped_rows == 1  # lo scarto NON raddoppia l'osservazione del run

    # Un secondo giro osserva di nuovo (voluto) ma sempre una riga per run.
    assert main([*ARGS, "--min-score", "60"]) == 0
    with closing(sqlite3.connect(db_path)) as conn:
        skipped_rows = conn.execute(
            "SELECT COUNT(*) FROM price_observations WHERE item_id = 2"
        ).fetchone()[0]
        distinct_runs = conn.execute(
            "SELECT COUNT(DISTINCT observed_at) FROM price_observations WHERE item_id = 2"
        ).fetchone()[0]
    assert skipped_rows == distinct_runs == 2


def test_stats_and_purge_stay_coherent_after_skips(
    monkeypatch: pytest.MonkeyPatch, db_path: Path
) -> None:
    install_fake_estimate(monkeypatch)
    set_pages(monkeypatch, [make_item(1, price="75.0"), make_item(2, price="40.0")])
    assert main([*ARGS, "--min-score", "60"]) == 0

    with closing(get_connection(db_path)) as conn:
        price_repo = PriceRepository(conn)
        item_repo = ItemRepository(conn)
        # lo storico conta ENTRAMBI gli item, scartato incluso
        assert sum(s.observations for s in price_repo.stats()) == 2
        assert price_repo.count_observations() == 2

        # il purge non resuscita né perde nulla: soglia ampia = nessuna perdita
        assert price_repo.purge_observations_older_than(365) == 0
        assert item_repo.purge_older_than(365) == 0
        assert price_repo.count_observations() == 2
        assert item_repo.count_unnotified() == 0  # lo scartato resta fuori coda


# ------------------------------------------ (h-legacy) crash a metà coda


def test_crash_mid_queue_preserves_already_sent(
    monkeypatch: pytest.MonkeyPatch, db_path: Path
) -> None:
    set_pages(monkeypatch, [make_item(1), make_item(2), make_item(3)])

    crash_after = 2
    original_send = FakeNotifier.send_item

    async def crashing_send(self: FakeNotifier, item: Item, estimate: object = None) -> None:
        if len(FakeNotifier.sent_ids) >= crash_after:
            raise RuntimeError("processo morto a metà coda")
        await original_send(self, item, estimate)

    monkeypatch.setattr(FakeNotifier, "send_item", crashing_send)

    with pytest.raises(RuntimeError, match="metà coda"):
        main(ARGS)

    # I 2 inviati PRIMA del crash sono marcati (mark per singolo item),
    # il terzo resta NULL e ripartirà al giro successivo.
    state = notified_map(db_path)
    assert sum(ts is not None for ts in state.values()) == 2
    assert sum(ts is None for ts in state.values()) == 1
