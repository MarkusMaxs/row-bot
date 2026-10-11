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
"""

from __future__ import annotations

import logging
import time

from row_bot import install_layout
from row_bot.install_layout import InstallKind, InstallLayout

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class SwitchError(RuntimeError):
    """The install has nothing valid to switch to."""


def runnable_version(layout: InstallLayout, version: str | None) -> bool:
    """True when ``versions/<version>`` (or the macOS seed) can be started."""
    if not version or not install_layout.is_valid_version_name(version):
        return False
    if layout.kind is InstallKind.MACOS and layout.seed and version == layout.version:
        return True
    if layout.root is None:
        return False
    return install_layout.version_runnable(layout.root, layout.kind, version)


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


def switch_to_ready(layout: InstallLayout, *, expected: str | None = None) -> str:
    """Make the ready version active; the next start runs it. Returns it."""
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
    state.extra["switched_at"] = _now_iso()
    install_layout.write_state(layout.root, state)
    if layout.kind is InstallKind.LINUX:
        install_layout.set_linux_current(layout.root, version)
    logger.info("Switched the install from %s to %s; it starts at the next launch", old, version)
    return version


def apply_on_quit(layout: InstallLayout | None = None) -> str | None:
    """Called while Row-Bot quits: switch to a ready version, never raise."""
    try:
        current = layout or install_layout.detect()
        if ready_version(current) is None:
            return None
        return switch_to_ready(current)
    except Exception:
        logger.warning("Could not apply the ready update while quitting", exc_info=True)
        return None


def rollback(layout: InstallLayout) -> str:
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
    install_layout.write_state(layout.root, state)
    if layout.kind is InstallKind.LINUX:
        install_layout.set_linux_current(layout.root, target)
    return target


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
    "SwitchError",
    "apply_on_quit",
    "cleanup_old_versions",
    "ready_version",
    "rollback",
    "runnable_version",
    "switch_to_ready",
]
