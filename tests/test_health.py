"""Tests for failure notification: cooldown, recovery, Telegram-is-down case."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

import vintedbot.health
from vintedbot.config import Settings
from vintedbot.health import HealthReporter, HealthState, failure_signature
from vintedbot.notifier import TelegramError

if TYPE_CHECKING:
    from pathlib import Path

TOKEN = "123456789:AAfaketokenfaketokenfaketoken"


class FakeNotifier:
    sent: list[str] = []
    fail_with: Exception | None = None

    def __init__(self, settings: object = None, **kwargs: object) -> None:
        pass

    async def __aenter__(self) -> FakeNotifier:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def send_text(self, text: str) -> None:
        if FakeNotifier.fail_with is not None:
            raise FakeNotifier.fail_with
        FakeNotifier.sent.append(text)


@pytest.fixture(autouse=True)
def _fake_notifier(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeNotifier.sent = []
    FakeNotifier.fail_with = None
    monkeypatch.setattr(vintedbot.health, "TelegramNotifier", FakeNotifier)


def make_settings(tmp_path: Path, **overrides: object) -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        data_dir=tmp_path,
        telegram_bot_token=TOKEN,
        telegram_chat_id="424242",
        **overrides,
    )


def boom(message: str = "crollo") -> Exception:
    try:
        raise RuntimeError(message)
    except RuntimeError as exc:
        return exc


# --------------------------------------------------- (h) cooldown anti-spam


async def test_first_failure_notifies(tmp_path: Path) -> None:
    reporter = HealthReporter(make_settings(tmp_path))

    assert await reporter.report_failure(boom(), context="run-all") is True
    assert len(FakeNotifier.sent) == 1
    assert "esecuzione fallita" in FakeNotifier.sent[0]
    assert "run-all" in FakeNotifier.sent[0]


async def test_same_failure_within_cooldown_is_silent(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    error = boom()

    assert await HealthReporter(settings).report_failure(error) is True
    # stessa firma, 1 ora dopo, cooldown 6h
    later = datetime.now(tz=UTC) + timedelta(hours=1)
    assert await HealthReporter(settings, now=later).report_failure(error) is False
    assert len(FakeNotifier.sent) == 1


async def test_same_failure_after_cooldown_notifies_again(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    error = boom()

    await HealthReporter(settings).report_failure(error)
    later = datetime.now(tz=UTC) + timedelta(hours=7)
    assert await HealthReporter(settings, now=later).report_failure(error) is True
    assert len(FakeNotifier.sent) == 2


async def test_different_failure_notifies_immediately(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)

    await HealthReporter(settings).report_failure(boom("primo"))
    other = ValueError("altro guasto")
    try:
        raise other
    except ValueError as exc:
        assert await HealthReporter(settings).report_failure(exc) is True
    assert len(FakeNotifier.sent) == 2


def test_signature_distinguishes_type_and_location() -> None:
    first = failure_signature(boom())
    second = failure_signature(boom())
    assert first == second  # stessa riga, stessa firma
    assert first.startswith("RuntimeError@")


# ------------------------------------------------------- (i) ripresa


async def test_recovery_message_after_failures(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    for _ in range(3):
        await HealthReporter(settings).report_failure(boom())
    FakeNotifier.sent = []

    assert await HealthReporter(settings).report_success() is True

    assert len(FakeNotifier.sent) == 1
    assert "tornato operativo dopo 3 esecuzioni fallite" in FakeNotifier.sent[0]
    # lo stato è azzerato: un secondo successo non riannuncia nulla
    assert await HealthReporter(settings).report_success() is False
    assert len(FakeNotifier.sent) == 1


async def test_no_recovery_message_when_nothing_was_broken(tmp_path: Path) -> None:
    assert await HealthReporter(make_settings(tmp_path)).report_success() is False
    assert FakeNotifier.sent == []


async def test_singular_wording_for_one_failure(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    await HealthReporter(settings).report_failure(boom())
    FakeNotifier.sent = []

    await HealthReporter(settings).report_success()
    assert "1 esecuzione fallita" in FakeNotifier.sent[0]


# ----------------------------------------- (j) Telegram è il guasto


async def test_telegram_failure_is_never_notified_over_telegram(tmp_path: Path) -> None:
    reporter = HealthReporter(make_settings(tmp_path))
    try:
        raise TelegramError("token invalido", status_code=401)
    except TelegramError as exc:
        assert await reporter.report_failure(exc) is False

    assert FakeNotifier.sent == []  # nessun tentativo


async def test_network_failure_towards_telegram_is_not_notified(tmp_path: Path) -> None:
    reporter = HealthReporter(make_settings(tmp_path))
    try:
        raise ConnectionError("cannot reach api.telegram.org")
    except ConnectionError as exc:
        assert await reporter.report_failure(exc) is False
    assert FakeNotifier.sent == []


async def test_send_failure_does_not_propagate(tmp_path: Path) -> None:
    FakeNotifier.fail_with = TelegramError("rete giù")
    reporter = HealthReporter(make_settings(tmp_path))

    assert await reporter.report_failure(boom()) is False  # nessun loop di errori
    assert reporter.state.consecutive_failures == 1


async def test_no_credentials_means_no_send(tmp_path: Path) -> None:
    settings = Settings(_env_file=None, data_dir=tmp_path)  # type: ignore[call-arg]
    assert await HealthReporter(settings).report_failure(boom()) is False
    assert FakeNotifier.sent == []


# ------------------------------------------------------------- stato


def test_state_survives_a_corrupt_file(tmp_path: Path) -> None:
    path = tmp_path / "health.json"
    path.write_text("{non json", encoding="utf-8")

    state = HealthState.load(path)  # nessuna eccezione: si riparte puliti
    assert state.consecutive_failures == 0
    assert state.notified_at == {}


async def test_state_is_persisted_between_processes(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    await HealthReporter(settings).report_failure(boom())

    # nuovo reporter = nuovo "processo": rilegge da disco
    assert HealthReporter(settings).state.consecutive_failures == 1
