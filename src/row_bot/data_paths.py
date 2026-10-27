"""Shared filesystem paths for local app data during the Row-Bot rebrand."""

from __future__ import annotations

import os
from pathlib import Path

from row_bot.brand import APP_DATA_DIR_ENV, default_data_dir


def get_row_bot_target_data_dir(*, create: bool = True) -> Path:
    """Return the canonical Row-Bot target data directory."""
    path = Path(os.environ.get(APP_DATA_DIR_ENV) or default_data_dir())
    path = path.expanduser()
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def get_row_bot_data_dir(*, create: bool = True) -> Path:
    """Return the currently active Row-Bot data directory."""
    return get_row_bot_target_data_dir(create=create)


def private_data_dirs() -> list[Path]:
    """Row-Bot's data folder, and the server secrets folder when one is in use (a self-hosted install's keys)."""
    from row_bot.secret_store import server_secrets_dir

    folders = [get_row_bot_data_dir(create=False)]
    secrets_dir = server_secrets_dir()
    if secrets_dir is not None:
        folders.append(secrets_dir)
    return folders


def is_private_data_path(path: str | os.PathLike[str]) -> bool:
    """True when *path* is Row-Bot's data folder or the server secrets folder, or inside one of them."""
    try:
        resolved = Path(path).expanduser().resolve()
        folders = [folder.expanduser().resolve() for folder in private_data_dirs()]
    except (OSError, ValueError):
        return False
    return any(resolved == folder or folder in resolved.parents for folder in folders)


# Parts of the data folder that hold tools Row-Bot downloaded and results it made, not conversations,
# settings, approvals or keys. A command may use them like any other folder, by the approval level
# (5.3.1 A1): the managed ffmpeg, recordings and exported media, logs.
SHARED_DATA_FOLDERS = frozenset({
    "ffmpeg", "runtimes", "node", "media", "recordings", "exports", "downloads", "channel_outputs",
    "vision-frames", "logs", "font_cache", "published",
})


def shared_data_folder(relative: str) -> bool:
    """True when *relative* (a path inside the data folder, with / separators) lies in a shared part of it."""
    first = relative.strip("/").split("/", 1)[0]
    return first in SHARED_DATA_FOLDERS


def get_tasks_db_path(*, create_parent: bool = True) -> Path:
    """Return the task database path."""
    data_dir = get_row_bot_data_dir(create=create_parent)
    return data_dir / "tasks.db"


def get_memory_db_path(*, create_parent: bool = True) -> Path:
    """Return the memory database path."""
    data_dir = get_row_bot_data_dir(create=create_parent)
    return data_dir / "memory.db"


def get_threads_db_path(*, create_parent: bool = True) -> Path:
    """Return the thread metadata/checkpoint database path."""
    data_dir = get_row_bot_data_dir(create=create_parent)
    return data_dir / "threads.db"


def get_usage_db_path(*, create_parent: bool = True) -> Path:
    """Return the local model-usage ledger path (token counts only)."""
    data_dir = get_row_bot_data_dir(create=create_parent)
    return data_dir / "usage.db"


def get_access_db_path(*, create_parent: bool = True) -> Path:
    """Return the access database path.

    The physical filename remains ``mobile.db`` for a safe in-place migration
    of existing companion devices.
    """
    data_dir = get_row_bot_data_dir(create=create_parent)
    return data_dir / "mobile.db"


def get_mobile_db_path(*, create_parent: bool = True) -> Path:
    """Compatibility alias for :func:`get_access_db_path`."""
    return get_access_db_path(create_parent=create_parent)


def describe_data_paths() -> dict[str, str]:
    """Return support-friendly local data paths."""
    data_dir = get_row_bot_data_dir(create=False)
    return {
        "data_dir": str(data_dir),
        "tasks_db": str(data_dir / "tasks.db"),
        "memory_db": str(data_dir / "memory.db"),
        "threads_db": str(data_dir / "threads.db"),
        "access_db": str(data_dir / "mobile.db"),
        "mobile_db": str(data_dir / "mobile.db"),
        "logs_dir": str(data_dir / "logs"),
    }
