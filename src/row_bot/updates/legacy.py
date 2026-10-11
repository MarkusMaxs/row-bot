"""The earlier Row-Bot installed for all users (Windows, 5.0/5.1).

The 5.2 installer removes it with one administrator prompt. When that prompt
was declined, the installer records ``legacy_install_pending`` in
``state.json`` and Settings offers the removal once more, with the same
uninstaller and the same prompt. Nothing here runs without that explicit
action, and the prompt itself is Windows' own.
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
_UNINSTALL_ARGUMENTS = "/VERYSILENT /SUPPRESSMSGBOXES /NORESTART"


def _registry_value(name: str) -> tuple[bool, str]:
    """Whether the per-machine entry exists, and one of its values."""
    if os.name != "nt":
        return False, ""
    import winreg

    for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, LEGACY_UNINSTALL_KEY, 0, winreg.KEY_READ | view) as key:
                try:
                    value, _kind = winreg.QueryValueEx(key, name)
                except OSError:
                    value = ""
                return True, str(value or "")
        except OSError:
            continue
    return False, ""


def legacy_install_present() -> bool:
    return _registry_value("UninstallString")[0]


def recorded(layout: InstallLayout) -> bool:
    if layout.kind is not install_layout.InstallKind.WINDOWS or layout.root is None:
        return False
    state = install_layout.read_state(layout.root)
    return bool(state is not None and state.extra.get(STATE_KEY))


def pending(layout: InstallLayout, *, present=legacy_install_present) -> bool:  # noqa: ANN001
    """Offer the removal: the installer recorded it and the old copy is still there."""
    return recorded(layout) and bool(present())


def clear(layout: InstallLayout) -> None:
    if layout.root is None:
        return
    state = install_layout.read_state(layout.root)
    if state is not None and STATE_KEY in state.extra:
        state.extra.pop(STATE_KEY, None)
        install_layout.write_state(layout.root, state)


def _uninstaller() -> str:
    _present, value = _registry_value("UninstallString")
    value = value.strip()
    if value.startswith('"'):
        try:
            value = shlex.split(value, posix=False)[0].strip('"')
        except ValueError:
            value = value.strip('"')
    return value


def start_removal(layout: InstallLayout) -> bool:
    """Start the old uninstaller elevated (Windows asks the person). True if started."""
    if os.name != "nt" or not recorded(layout):
        return False
    if not legacy_install_present():
        clear(layout)
        return False
    import ctypes

    uninstaller = _uninstaller()
    if uninstaller and Path(uninstaller).is_file():
        program, arguments = uninstaller, _UNINSTALL_ARGUMENTS
    else:
        # Its folder is gone already: only the Apps & features entry is left.
        program = str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "reg.exe")
        arguments = f'delete "HKLM\\{LEGACY_UNINSTALL_KEY}" /f /reg:64'
    result = ctypes.windll.shell32.ShellExecuteW(None, "runas", program, arguments, None, 0)
    started = int(result) > 32
    logger.info("Removal of the earlier Row-Bot %s (%s)", "started" if started else "was not started", int(result))
    return started


__all__ = [
    "LEGACY_UNINSTALL_KEY",
    "STATE_KEY",
    "clear",
    "legacy_install_present",
    "pending",
    "recorded",
    "start_removal",
]
