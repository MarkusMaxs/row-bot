"""Switch an install to its staged version, go back, and tidy old versions.

Switching only rewrites ``state.json`` (and, on Linux, the ``current`` link):
``active`` becomes the ready version, the version that ran becomes
``previous``, and ``pending_verification`` is set so the native launcher can
go back by itself if the new version does not start (PLAN.md "Switching").
The running processes are untouched; the switch takes effect at the next
start, which is either now (the launcher exits with code 75 and is started
again) or when Row-Bot next quits and starts.

The module needs only ``install_layout`` and the standard library, so the
launcher can apply a ready update while it quits.

A switch followed by an immediate relaunch (Restart now, when idle, the tray's
Restart to Update, Repair) records ``restart_reason`` in ``state.json``: the
next start consumes it once, so goals left working carry on by themselves
after a restart Row-Bot made itself (decision D4,
``startup_state.last_exit_was_app_initiated``). A switch while Row-Bot quits
records nothing: the person quit, and the next start is theirs.
"""

from __future__ import annotations

import calendar
import logging
import os
import time
from collections.abc import Callable
from pathlib import Path

from row_bot import install_layout
from row_bot.install_layout import InstallKind, InstallLayout

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


#: macOS: the app bundle's version, recorded when the first update switches
#: away from it, so a staged version can still go back to the bundle.
SEED_VERSION_KEY = "seed_version"

#: ``{"reason": "update"|"repair", "version": <switched to>, "at": <ISO time>}``
#: (the macOS tray host writes the same).
RESTART_REASON_KEY = "restart_reason"
RESTART_REASONS = frozenset({"update", "repair"})
#: A relaunch takes seconds; an older marker belongs to a restart that didn't happen.
RESTART_REASON_MAX_AGE = 600.0


def _set_restart_reason(state: install_layout.InstallState, reason: str | None, version: str) -> None:
    if reason is None:
        state.extra.pop(RESTART_REASON_KEY, None)
        return
    if reason not in RESTART_REASONS:
        raise ValueError(f"unknown restart reason {reason!r}")
    state.extra[RESTART_REASON_KEY] = {"reason": reason, "version": version, "at": _now_iso()}


class SwitchError(RuntimeError):
    """The install has nothing valid to switch to."""


class InstallLock:
    """One Row-Bot at a time prepares or switches an install (across processes).

    ``R/.updates/engine.lock``, held with ``msvcrt.locking`` / ``fcntl.flock``;
    the OS releases it if the holder dies.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._handle = None

    @classmethod
    def for_install(cls, root: Path) -> "InstallLock":
        return cls(Path(root) / ".updates" / "engine.lock")

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            handle.seek(0, 2)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        self._handle = handle
        return True

    def release(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            handle.close()


def runnable_version(layout: InstallLayout, version: str | None) -> bool:
    """True when ``versions/<version>`` (or the macOS seed) can be started."""
    if not version or not install_layout.is_valid_version_name(version):
        return False
    if layout.kind is InstallKind.MACOS and layout.seed and version == layout.version:
        return True
    if layout.root is None:
        return False
    if install_layout.version_runnable(layout.root, layout.kind, version):
        return True
    if layout.kind is InstallKind.MACOS:
        # The bundle (the seed) lives outside versions/; the tray host starts it.
        state = install_layout.read_state(layout.root)
        return bool(state is not None and state.extra.get(SEED_VERSION_KEY) == version)
    return False


def ready_version(layout: InstallLayout) -> str | None:
    """The staged version waiting in ``state.json``, if it can still start."""
    if not layout.self_updating or layout.root is None:
        return None
    state = install_layout.load_state(layout.root, layout.kind)
    if state is None or not state.ready:
        return None
    version = state.ready.get("version")
    if version == state.active or not runnable_version(layout, version):
        return None
    return str(version)


def switch_to_ready(
    layout: InstallLayout, *, expected: str | None = None, restart_reason: str | None = None
) -> str:
    """Make the ready version active; the next start runs it. Returns it.

    ``restart_reason`` ("update") when Row-Bot relaunches right away.
    """
    if not layout.self_updating or layout.root is None:
        raise SwitchError("this install does not update itself")
    state = install_layout.load_state(layout.root, layout.kind)
    if state is None or not state.ready:
        raise SwitchError("no update is ready")
    version = str(state.ready.get("version") or "")
    if expected is not None and version != expected:
        raise SwitchError(f"{version or 'nothing'} is ready, not {expected}")
    if version == state.active or not runnable_version(layout, version):
        raise SwitchError(f"{version} cannot be started")
    old = state.active
    state.previous = old
    state.active = version
    state.ready = None
    state.pending_verification = True
    state.unverified_launches = 0
    state.extra.pop("verified_at", None)
    # Who switched what, so a later start can tell a rollback of this switch.
    state.extra["switched_at"] = _now_iso()
    state.extra["switched_from"] = old
    state.extra["switched_to"] = version
    _set_restart_reason(state, restart_reason, version)
    if layout.kind is InstallKind.MACOS and layout.seed and layout.version:
        state.extra[SEED_VERSION_KEY] = layout.version
    install_layout.write_state(layout.root, state)
    if layout.kind is InstallKind.LINUX:
        install_layout.set_linux_current(layout.root, version)
    logger.info("Switched the install from %s to %s; it starts at the next launch", old, version)
    return version


def switch_to_ready_locked(
    layout: InstallLayout, *, expected: str | None = None, restart_reason: str | None = None
) -> str | None:
    """:func:`switch_to_ready` under the install lock; None while another Row-Bot holds it."""
    from row_bot.updates.external import external_mode

    if external_mode() or not layout.self_updating or layout.root is None:
        return None  # the host applies updates (D11)
    lock = InstallLock.for_install(layout.root)
    if not lock.acquire():
        logger.info("Another Row-Bot is preparing or switching this install; not switching now")
        return None
    try:
        return switch_to_ready(layout, expected=expected, restart_reason=restart_reason)
    finally:
        lock.release()


def apply_on_quit(layout: InstallLayout | None = None) -> str | None:
    """Called while Row-Bot quits: switch to a ready version, never raise."""
    try:
        from row_bot.updates.external import external_mode

        if external_mode():
            return None  # the host applies updates (D11)
        current = layout or install_layout.detect()
        if ready_version(current) is None:
            return None
        return switch_to_ready_locked(current)
    except Exception:
        logger.warning("Could not apply the ready update while quitting", exc_info=True)
        return None


def rollback(layout: InstallLayout, *, restart_reason: str | None = None) -> str:
    """Repair: make ``previous`` active again (it starts at the next launch)."""
    if not layout.self_updating or layout.root is None:
        raise SwitchError("this install does not update itself")
    state = install_layout.load_state(layout.root, layout.kind)
    if state is None or not state.previous or state.previous == state.active:
        raise SwitchError("there is no earlier version to go back to")
    target = state.previous
    if not runnable_version(layout, target):
        raise SwitchError(f"{target} is no longer installed")
    failed = state.active
    state.active = target
    state.previous = failed
    state.ready = None
    # The earlier version ran before; if it no longer starts, the launcher
    # comes back to this one after two tries.
    state.pending_verification = True
    state.unverified_launches = 0
    state.last_rollback = {"from": failed, "to": target, "at": _now_iso(), "reason": "repair"}
    _set_restart_reason(state, restart_reason, target)
    install_layout.write_state(layout.root, state)
    if layout.kind is InstallKind.LINUX:
        install_layout.set_linux_current(layout.root, target)
    return target


def clear_restart_reason(layout: InstallLayout) -> None:
    """The relaunch didn't happen: the person's own restart is a normal start."""
    if not layout.self_updating or layout.root is None:
        return
    state = install_layout.read_state(layout.root)
    if state is not None and RESTART_REASON_KEY in state.extra:
        state.extra.pop(RESTART_REASON_KEY, None)
        install_layout.write_state(layout.root, state)


def consume_restart_reason(
    layout: InstallLayout | None = None, *, now: Callable[[], float] = time.time
) -> str | None:
    """Why Row-Bot restarted itself into this version ("update", "repair"), once.

    The marker is removed whatever it says. It counts only when it names the
    version now running (not one the launcher rolled back from) and was
    written moments ago; anything else is a normal start.
    """
    current = layout or install_layout.detect()
    if not current.self_updating or current.root is None:
        return None
    state = install_layout.read_state(current.root)
    if state is None or RESTART_REASON_KEY not in state.extra:
        return None
    marker = state.extra.pop(RESTART_REASON_KEY, None)
    install_layout.write_state(current.root, state)
    if not isinstance(marker, dict) or marker.get("reason") not in RESTART_REASONS:
        return None
    if marker.get("version") != current.version:
        return None
    try:
        written = calendar.timegm(time.strptime(str(marker.get("at")), "%Y-%m-%dT%H:%M:%SZ"))
    except (TypeError, ValueError, OverflowError):
        return None
    if not 0 <= now() - written <= RESTART_REASON_MAX_AGE:
        return None
    return str(marker["reason"])


def held_back(layout: InstallLayout) -> tuple[str, str] | None:
    """The version this install went back from, and why ("rolled_back" or "repair").

    Row-Bot does not prepare that version again by itself: it failed to start
    (the launcher rolled it back) or the person chose to leave it (Repair).
    Once the running version is that one again, nothing is held back.
    """
    if not layout.self_updating or layout.root is None:
        return None
    state = install_layout.load_state(layout.root, layout.kind)
    rollback = state.last_rollback if state is not None else None
    if not isinstance(rollback, dict):
        return None
    version = rollback.get("from")
    if not install_layout.is_valid_version_name(version) or version == layout.version:
        return None
    reason = "repair" if rollback.get("reason") == "repair" else "rolled_back"
    return str(version), reason


def cleanup_old_versions(layout: InstallLayout) -> list[str]:
    """Delete versions other than active, previous and ready (after a verified start).

    Does nothing while the running version waits for verification: the native
    launcher may still need ``previous``. Deleting a version can take a minute
    on Windows, so callers run this in the background.
    """
    if not layout.self_updating or layout.root is None:
        return []
    state = install_layout.load_state(layout.root, layout.kind)
    if state is None or state.pending_verification or state.active != layout.version:
        return []
    keep = {state.active, state.previous or "", (state.ready or {}).get("version") or ""}
    removed = install_layout.prune_versions(layout.root, layout.kind, keep)
    if removed:
        logger.info("Removed old Row-Bot versions: %s", ", ".join(removed))
    return removed


__all__ = [
    "RESTART_REASON_KEY",
    "InstallLock",
    "SwitchError",
    "apply_on_quit",
    "cleanup_old_versions",
    "clear_restart_reason",
    "consume_restart_reason",
    "held_back",
    "ready_version",
    "rollback",
    "runnable_version",
    "switch_to_ready",
    "switch_to_ready_locked",
]
