"""Where VintedBot keeps its data, and why it must not be a synced folder.

The database, the logs and the lock file live in a per-user data
directory OUTSIDE any cloud-synced tree. This is not hygiene theatre:
OneDrive (and Dropbox, and Google Drive) re-upload the ``.db`` on every
write and can hold it open while doing so, which surfaces as
intermittent ``database is locked`` errors and, with WAL, a real risk of
losing or corrupting the ``-wal`` sidecar.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

#: Folder-name fragments that mean "this path is continuously synced".
CLOUD_SYNC_MARKERS = (
    "onedrive",
    "dropbox",
    "google drive",
    "googledrive",
    "gdrive",
    "icloud",
    "yandexdisk",
)


def default_data_dir() -> Path:
    """Per-user data directory, resolved per platform.

    Windows: ``%LOCALAPPDATA%\\VintedBot`` (LocalAppData is deliberately
    NOT roamed/synced). Elsewhere: ``$XDG_DATA_HOME/vintedbot`` or
    ``~/.local/share/vintedbot``.
    """
    if sys.platform == "win32":
        local_app_data = os.environ.get("LOCALAPPDATA")
        base = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
        return base / "VintedBot"

    xdg_data_home = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg_data_home) if xdg_data_home else Path.home() / ".local" / "share"
    return base / "vintedbot"


def cloud_sync_marker(path: Path) -> str | None:
    """Return the cloud-sync service a path seems to live under, else None."""
    parts = [part.lower() for part in path.expanduser().absolute().parts]
    for part in parts:
        for marker in CLOUD_SYNC_MARKERS:
            if marker in part:
                return marker
    return None
