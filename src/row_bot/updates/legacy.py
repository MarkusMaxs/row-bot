"""The earlier Row-Bot installed for all users (Windows, 5.0/5.1).

The 5.2 installer removes it with one administrator prompt. When that prompt
was declined, the installer records ``legacy_install_pending`` in
``state.json`` and Settings offers the removal once more, with the same
uninstaller and the same prompt. Nothing here runs without that explicit
action, and the prompt itself is Windows' own.

Only ``HKEY_LOCAL_MACHINE`` is read (both registry views): that entry can only
be written by an administrator, so its ``UninstallString`` is trusted as much
as the old install itself. The uninstaller always gets the same fixed
arguments.
"""

from __future__ import annotations

import logging
import os
import shlex
from pathlib import Path

from row_bot import install_layout
from row_bot.install_layout import InstallLayout

logger = logging.getLogger(__name__)

STATE_KEY = "legacy_install_pending"
LEGACY_UNINSTALL_KEY = (
    r"Software\Microsoft\Windows\CurrentVersion\Uninstall\{A1B2C3D4-E5F6-7890-ABCD-EF1234567890}_is1"
)
UNINSTALL_ARGUMENTS = "/VERYSILENT /SUPPRESSMSGBOXES /NORESTART"


def _is_windows() -> bool:
    return os.name == "nt"


def _registry_entry() -> dict[str, object] | None:
    """The per-machine uninstall entry: its values and which registry view held it."""
    if not _is_windows():
        return None
    import winreg

    for view, label in ((winreg.KEY_WOW64_64KEY, "64"), (winreg.KEY_WOW64_32KEY, "32")):
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, LEGACY_UNINSTALL_KEY, 0, winreg.KEY_READ | view) as key:
                values: dict[str, object] = {"view": label}
                for name in ("UninstallString", "InstallLocation", "Inno Setup: App Path"):
                    try:
                        values[name], _kind = winreg.QueryValueEx(key, name)
                    except OSError:
                        values[name] = ""
                return values
        except OSError:
            continue
    return None


def _shell_execute(program: str, arguments: str) -> int:
    """ShellExecuteW "runas": Windows shows its elevation prompt. > 32 means started."""
    import ctypes

    return int(ctypes.windll.shell32.ShellExecuteW(None, "runas", program, arguments, None, 0))


def legacy_install_present() -> bool:
    return _registry_entry() is not None


def recorded(layout: InstallLayout) -> bool:
    if layout.kind is not install_layout.InstallKind.WINDOWS or layout.root is None:
        return False
    state = install_layout.read_state(layout.root)
    return bool(state is not None and state.extra.get(STATE_KEY))


def pending(layout: InstallLayout) -> bool:
    """Offer the removal: the installer recorded it and the old copy is still there."""
    return recorded(layout) and legacy_install_present()


def clear(layout: InstallLayout) -> None:
    if layout.root is None:
        return
    state = install_layout.read_state(layout.root)
    if state is not None and STATE_KEY in state.extra:
        state.extra.pop(STATE_KEY, None)
        install_layout.write_state(layout.root, state)


def uninstaller_path(uninstall_string: str) -> str:
    """The program of an ``UninstallString`` (quoted or not), without its arguments."""
    value = (uninstall_string or "").strip()
    if value.startswith('"'):
        try:
            return shlex.split(value, posix=False)[0].strip('"')
        except ValueError:
            return value.strip('"').split('"', 1)[0]
    return value


def start_removal(layout: InstallLayout) -> bool:
    """Start the old uninstaller elevated (Windows asks the person). True if started.

    Blocks while Windows shows its prompt, so callers run it off any lock.
    """
    if not _is_windows() or not recorded(layout):
        return False
    entry = _registry_entry()
    if entry is None:
        clear(layout)
        return False
    uninstaller = uninstaller_path(str(entry.get("UninstallString") or ""))
    if uninstaller and Path(uninstaller).is_file():
        program, arguments = uninstaller, UNINSTALL_ARGUMENTS
    else:
        folder = str(entry.get("InstallLocation") or entry.get("Inno Setup: App Path") or "").strip()
        if not folder and uninstaller:
            folder = str(Path(uninstaller).parent)
        if folder and Path(folder).exists():
            # The uninstaller is missing but the program is still there:
            # deleting the entry would hide it. Leave it to Windows Settings.
            logger.warning("The earlier Row-Bot's uninstaller is missing but %s is still there", folder)
            return False
        # Its folder is gone already: only the Apps & features entry is left.
        program = str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "reg.exe")
        arguments = f'delete "HKLM\\{LEGACY_UNINSTALL_KEY}" /f /reg:{entry.get("view") or "64"}'
    result = _shell_execute(program, arguments)
    started = result > 32
    logger.info("Removal of the earlier Row-Bot %s (%s)", "started" if started else "was not started", result)
    return started


__all__ = [
    "LEGACY_UNINSTALL_KEY",
    "STATE_KEY",
    "UNINSTALL_ARGUMENTS",
    "clear",
    "legacy_install_present",
    "pending",
    "recorded",
    "start_removal",
    "uninstaller_path",
]
