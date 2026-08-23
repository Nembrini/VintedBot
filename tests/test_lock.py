"""Tests for the single-instance lock: exclusivity and crash safety."""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING

import pytest

from vintedbot.lock import LockBusyError, SingleInstanceLock, probe_lock

if TYPE_CHECKING:
    from pathlib import Path


def test_second_instance_is_refused_while_first_holds(tmp_path: Path) -> None:
    path = tmp_path / "vintedbot.lock"

    with SingleInstanceLock(path):
        second = SingleInstanceLock(path)
        with pytest.raises(LockBusyError) as excinfo:
            second.__enter__()
        assert "già in esecuzione" in str(excinfo.value)


def test_lock_is_reusable_after_a_clean_release(tmp_path: Path) -> None:
    path = tmp_path / "vintedbot.lock"

    with SingleInstanceLock(path):
        pass
    with SingleInstanceLock(path):  # nessun lock orfano
        pass


def test_lock_released_when_holder_dies_without_cleanup(tmp_path: Path) -> None:
    """Simula una morte anomala: fd chiuso dall'OS, nessun __exit__."""
    path = tmp_path / "vintedbot.lock"

    crashed = SingleInstanceLock(path)
    crashed.__enter__()
    # Nessun __exit__: emuliamo il processo che sparisce chiudendo il
    # descrittore come farebbe l'OS.
    os.close(crashed._fd)  # type: ignore[arg-type]  # noqa: SLF001
    crashed._fd = None  # noqa: SLF001

    with SingleInstanceLock(path):  # deve partire regolarmente
        pass


def test_holder_diagnostics_are_readable_from_outside(tmp_path: Path) -> None:
    path = tmp_path / "vintedbot.lock"

    with SingleInstanceLock(path):
        # il payload resta leggibile: il lock è su un byte oltre EOF
        holder = json.loads(path.read_text(encoding="utf-8"))
        assert holder["pid"] == os.getpid()
        assert holder["started_at"]

        with pytest.raises(LockBusyError) as excinfo:
            SingleInstanceLock(path).__enter__()
        assert excinfo.value.holder["pid"] == os.getpid()


# ------------------------------------------------------- probe di sola lettura


def test_probe_reports_a_free_lock_without_creating_the_file(tmp_path: Path) -> None:
    path = tmp_path / "vintedbot.lock"

    status = probe_lock(path)

    assert status.held is False
    assert status.holder == {}
    assert not path.exists()  # una diagnosi non deve lasciare artefatti


def test_probe_reports_the_holder_while_locked(tmp_path: Path) -> None:
    path = tmp_path / "vintedbot.lock"

    with SingleInstanceLock(path):
        status = probe_lock(path)

    assert status.held is True
    assert status.holder["pid"] == os.getpid()


def test_probe_does_not_rewrite_the_diagnostics(tmp_path: Path) -> None:
    """Acquiring the lock stamps a new pid: probing must not do that."""
    path = tmp_path / "vintedbot.lock"
    with SingleInstanceLock(path):
        pass
    before = path.read_bytes()

    assert probe_lock(path).held is False

    assert path.read_bytes() == before


def test_probe_leaves_the_lock_available(tmp_path: Path) -> None:
    path = tmp_path / "vintedbot.lock"
    with SingleInstanceLock(path):
        pass

    probe_lock(path)

    with SingleInstanceLock(path):  # il probe non deve trattenere nulla
        pass
