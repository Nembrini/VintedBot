"""Failure reporting for unattended runs: notify once, then shut up.

Two requirements pull against each other — "tell me when the bot breaks"
and "do not bury me in messages" — so a failure is notified at most once
per ``error_notify_cooldown_hours`` per *signature* (exception type plus
the code location that raised it). When a run finally succeeds after
failures, a short recovery message closes the loop: silence alone would
never tell you the bot is back.

State lives in a JSON file in the data dir, deliberately NOT in the
database: the failures worth reporting most loudly are the ones where the
database is locked, missing or corrupt, and a tracker that needs the DB
would be unavailable in exactly those cases.

Telegram failures are never reported over Telegram — logging them is all
we can honestly do.
"""

from __future__ import annotations

import json
import traceback
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import structlog

from vintedbot.notifier import TelegramError, TelegramNotifier

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from vintedbot.config import Settings

logger = structlog.get_logger(__name__)


@dataclass
class HealthState:
    """Persisted failure bookkeeping."""

    consecutive_failures: int = 0
    #: signature -> ISO timestamp of the last notification sent for it
    notified_at: dict[str, str] = field(default_factory=dict)
    #: Summary of the most recent run, for `doctor` to report. Kept here
    #: rather than parsed back out of the log file: log lines are rendered
    #: for humans and their shape changes with VINTEDBOT_LOG_JSON, while
    #: this file is already the one designed to outlive database trouble.
    last_run: dict[str, object] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> HealthState:
        """Read the state; a missing or damaged file starts from scratch."""
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        if not isinstance(raw, dict):
            return cls()
        notified = raw.get("notified_at")
        last_run = raw.get("last_run")
        return cls(
            consecutive_failures=int(raw.get("consecutive_failures", 0) or 0),
            notified_at={
                str(key): str(value)
                for key, value in (notified.items() if isinstance(notified, dict) else ())
            },
            last_run=dict(last_run) if isinstance(last_run, dict) else {},
        )

    def save(self, path: Path) -> None:
        """Persist the state, best effort: never break a run over telemetry."""
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {
                        "consecutive_failures": self.consecutive_failures,
                        "notified_at": self.notified_at,
                        "last_run": self.last_run,
                    },
                    indent=1,
                ),
                encoding="utf-8",
            )
        except OSError:  # pragma: no cover
            logger.warning("health_state_save_failed", path=str(path))


def failure_signature(exc: BaseException) -> str:
    """Identify a failure as 'exception type at code location'.

    The location is the innermost frame of the traceback, so the same bug
    keeps one signature across runs while a different bug gets its own.
    """
    frames = traceback.extract_tb(exc.__traceback__)
    if frames:
        last = frames[-1]
        location = f"{last.filename.rsplit('/', 1)[-1].rsplit(chr(92), 1)[-1]}:{last.lineno}"
    else:
        location = "unknown"
    return f"{type(exc).__name__}@{location}"


def is_telegram_failure(exc: BaseException) -> bool:
    """True when Telegram itself is the broken part — do not notify over it."""
    current: BaseException | None = exc
    while current is not None:
        if isinstance(current, TelegramError):
            return True
        if "api.telegram.org" in str(current):
            return True
        current = current.__cause__ or current.__context__
    return False


class HealthReporter:
    """Decides what to tell the user about failures, and remembers it."""

    def __init__(self, settings: Settings, *, now: datetime | None = None) -> None:
        self._settings = settings
        self._path = settings.health_path
        self._now = now or datetime.now(tz=UTC)
        self.state = HealthState.load(self._path)

    async def report_failure(self, exc: BaseException, *, context: str | None = None) -> bool:
        """Record a failed run and notify it if the cooldown allows.

        Returns True when a Telegram message was actually sent. The full
        traceback goes to the log only — the message carries the exception
        type, the context, and nothing else.
        """
        self.state.consecutive_failures += 1
        signature = failure_signature(exc)

        logger.error(
            "run_failed",
            error_type=type(exc).__name__,
            error=str(exc),
            signature=signature,
            context=context,
            consecutive_failures=self.state.consecutive_failures,
            exc_info=exc,
        )

        notified = False
        if is_telegram_failure(exc):
            logger.info("failure_not_notified_telegram_is_the_problem", signature=signature)
        elif self._within_cooldown(signature):
            logger.info(
                "failure_not_notified_cooldown",
                signature=signature,
                cooldown_hours=self._settings.error_notify_cooldown_hours,
            )
        else:
            notified = await self._send(
                f"⚠️ VintedBot: esecuzione fallita\n"
                f"Errore: {type(exc).__name__}"
                + (f"\nDove: {context}" if context else "")
                + "\nDettagli nel file di log."
            )
            if notified:
                self.state.notified_at[signature] = self._now.isoformat()

        self.state.save(self._path)
        return notified

    async def report_success(self) -> bool:
        """Record a successful run; announce the recovery if we were down."""
        previous_failures = self.state.consecutive_failures
        self.state.consecutive_failures = 0

        notified = False
        if previous_failures > 0:
            plural = "esecuzione fallita" if previous_failures == 1 else "esecuzioni fallite"
            notified = await self._send(
                f"✅ VintedBot è tornato operativo dopo {previous_failures} {plural}."
            )
            logger.info("recovery_notified", previous_failures=previous_failures)

        self.state.save(self._path)
        return notified

    def record_last_run(
        self,
        *,
        outcome: str,
        exit_code: int,
        duration_seconds: float,
        trigger: str,
        totals: Mapping[str, int] | None = None,
    ) -> None:
        """Store how the run ended, so `doctor` can say it without reading logs."""
        self.state.last_run = {
            "finished_at": self._now.isoformat(),
            "outcome": outcome,
            "exit_code": exit_code,
            "duration_seconds": round(duration_seconds, 1),
            "trigger": trigger,
            **({} if totals is None else {key: int(value) for key, value in totals.items()}),
        }
        self.state.save(self._path)

    # ------------------------------------------------------------ internals

    def _within_cooldown(self, signature: str) -> bool:
        last_iso = self.state.notified_at.get(signature)
        if last_iso is None:
            return False
        try:
            last = datetime.fromisoformat(last_iso)
        except ValueError:
            return False
        cooldown = timedelta(hours=self._settings.error_notify_cooldown_hours)
        return self._now - last < cooldown

    async def _send(self, text: str) -> bool:
        """Send a short status message; a failing send is logged, never raised."""
        if self._settings.telegram_bot_token is None or not self._settings.telegram_chat_id:
            logger.info("health_message_skipped_no_credentials")
            return False
        try:
            async with TelegramNotifier(self._settings) as notifier:
                await notifier.send_text(text)
        except (TelegramError, OSError) as exc:
            # Nessun loop di errori: se anche questo invio fallisce, resta nel log.
            logger.warning("health_message_send_failed", error=str(exc))
            return False
        return True
