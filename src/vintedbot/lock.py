"""Single-instance lock, crash-safe by construction.

The scheduler fires ``run-all`` every few minutes regardless of whether
the previous run finished; a second concurrent run would mean duplicate
notifications and concurrent writes. This module makes the second run
bow out.

The lock is an OS byte-range lock held on an open file descriptor
(``msvcrt.locking`` on Windows, ``fcntl.flock`` elsewhere) — NOT the mere
existence of a file. That distinction is the whole point: when a process
crashes, is killed, or dies with the machine, the OS closes its handles
and the lock disappears with them. A stale lock file can never wedge the
bot forever.

The lock byte sits far past EOF (offset 1 MiB) so the first bytes of the
file stay readable by everyone: they hold the PID and start time of the
holder, purely for diagnostics. Locking byte 0 instead would make the
file unreadable to other processes on Windows — verified, not assumed.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Self

import structlog

if TYPE_CHECKING:
    from pathlib import Path
    from types import TracebackType

logger = structlog.get_logger(__name__)

#: Offset of the locked byte: past any diagnostic payload, hence sparse.
_LOCK_OFFSET = 1 << 20


class LockBusyError(Exception):
    """Another instance already holds the lock.

    Attributes:
        holder: diagnostics read from the lock file (pid, started_at), if
            readable — best effort, never a reason to fail.
    """

    def __init__(self, message: str, holder: dict[str, object] | None = None) -> None:
        super().__init__(message)
        self.holder = holder or {}


if sys.platform == "win32":
    import msvcrt

    def _try_lock(fd: int) -> None:
        os.lseek(fd, _LOCK_OFFSET, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

    def _unlock(fd: int) -> None:
        os.lseek(fd, _LOCK_OFFSET, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

else:  # pragma: no cover - the project targets Windows; POSIX kept working
    import fcntl

    def _try_lock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


@dataclass(frozen=True)
class LockStatus:
    """Whether the lock is currently held, and by whom."""

    held: bool
    holder: dict[str, object]


def probe_lock(path: Path) -> LockStatus:
    """Report the lock state WITHOUT disturbing it.

    ``doctor`` must not answer "is anyone running?" by acquiring the lock:
    acquiring it rewrites the diagnostic payload with the prober's own PID
    and would leave a lie behind. So this takes the lock only long enough
    to learn that it was free, never writes, and never creates the file.
    """
    if not path.exists():
        return LockStatus(held=False, holder={})
    try:
        fd = os.open(path, os.O_RDWR)
    except OSError:
        # Unreadable for reasons of its own: report as free rather than
        # inventing a holder — the run itself will fail loudly if it matters.
        return LockStatus(held=False, holder={})
    try:
        _try_lock(fd)
    except OSError:
        return LockStatus(held=True, holder=_read_holder(path))
    else:
        _unlock(fd)
        return LockStatus(held=False, holder={})
    finally:
        os.close(fd)


def _read_holder(path: Path) -> dict[str, object]:
    """Best-effort diagnostics from the readable head of the lock file."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


class SingleInstanceLock:
    """Context manager granting exclusive execution rights.

    Usage::

        with SingleInstanceLock(path):
            ...  # only one process at a time gets here

    Raises:
        LockBusyError: on enter, when another live process holds the lock.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._fd: int | None = None

    def __enter__(self) -> Self:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._path, os.O_RDWR | os.O_CREAT)
        try:
            _try_lock(fd)
        except OSError as exc:
            holder = self._read_holder()
            os.close(fd)
            raise LockBusyError(
                f"un'altra istanza è già in esecuzione (lock: {self._path})", holder
            ) from exc

        self._fd = fd
        self._write_holder(fd)
        logger.debug("lock_acquired", path=str(self._path), pid=os.getpid())
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._fd is None:
            return
        try:
            _unlock(self._fd)
        except OSError:  # pragma: no cover - chiudere basta comunque
            pass
        finally:
            os.close(self._fd)
            self._fd = None
        logger.debug("lock_released", path=str(self._path))

    # ------------------------------------------------------------ internals

    def _write_holder(self, fd: int) -> None:
        """Store pid/start time in the readable head of the file."""
        payload = json.dumps(
            {"pid": os.getpid(), "started_at": datetime.now(tz=UTC).isoformat()}
        ).encode("utf-8")
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, payload)
            os.ftruncate(fd, len(payload))
        except OSError:  # pragma: no cover - diagnostica, mai fatale
            logger.debug("lock_diagnostics_write_failed", path=str(self._path))

    def _read_holder(self) -> dict[str, object]:
        return _read_holder(self._path)
