"""Where Row-Bot is installed, which version runs, and the install's ``state.json``.

An installed Row-Bot keeps one folder per version under an install root ``R``
and a small pointer file, ``R/state.json``:

- Windows: ``%LOCALAPPDATA%\\Programs\\Row-Bot`` holds ``Row-Bot.exe``,
  ``versions\\<v>\\{app,python}`` and ``state.json``.
- macOS: ``~/Library/Application Support/Row-Bot`` holds ``versions/<v>`` and
  ``state.json``. The app bundle's ``Contents/Resources/{app,python}`` is the
  "seed" version.
- Linux: ``$XDG_DATA_HOME/row-bot`` (``~/.local/share/row-bot``) holds
  ``releases/<v>``, the ``current`` link and ``state.json``. The ``current``
  link stays the source of truth for the active version.

The native launchers (``Row-Bot.exe``, the macOS tray host and the Linux
``bin/row-bot`` wrapper) read ``state.json``, apply :func:`begin_launch` (the
loop guard), start ``<active>/python`` with ``<active>/app/launcher.py`` and
start it again only when it exits with :data:`RELAUNCH_EXIT_CODE`. The Python
launcher clears ``pending_verification`` once ``/readyz`` answers
(:func:`verify_when_ready`). User data never lives under ``R``.

The module uses the standard library only, so it also runs as a script
(``python -I -S install_layout.py ...``) for the Linux wrapper and
``install.sh`` before anything else can be imported.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import platform
import re
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable, Mapping

#: The Python launcher exits with this code to mean "start me again": the
#: native launcher re-reads ``state.json`` and starts the active version.
#: Every other exit code ends the native launcher too.
RELAUNCH_EXIT_CODE = 75

STATE_FILE = "state.json"
STATE_SCHEMA = 1
VERSION_FILE = "version.json"
#: Launches in a row that never became ready before the native launcher goes
#: back to ``previous``.
MAX_UNVERIFIED_LAUNCHES = 2
#: A start that is not ready this long after launch counts as failed.
READY_TIMEOUT_SECONDS = 240.0

INSTALL_ROOT_ENV = "ROW_BOT_INSTALL_ROOT"
APP_ROOT_ENV = "ROW_BOT_APP_ROOT"
CONTAINERIZED_ENV = "ROW_BOT_CONTAINERIZED"
WINDOWS_ROOT_NAME = "Row-Bot"
MACOS_ROOT_NAME = "Row-Bot"
LINUX_ROOT_NAME = "row-bot"
LINUX_CURRENT_LINK = "current"

_VERSION_NAME = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._+-]{0,63}$")
_KNOWN_STATE_KEYS = frozenset(
    {
        "schema",
        "active",
        "previous",
        "ready",
        "pending_verification",
        "unverified_launches",
        "last_rollback",
    }
)


class InstallKind(StrEnum):
    WINDOWS = "windows"
    MACOS = "macos"
    LINUX = "linux"
    DEV = "dev"
    CONTAINER = "container"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class InstallLayout:
    """The install this process runs from.

    ``root`` is the install root ``R`` and ``version`` the folder name of the
    running version (the bundle's version for the macOS seed). Both are None
    for a source checkout, a container or anything unrecognised.
    """

    kind: InstallKind
    root: Path | None = None
    version: str | None = None
    version_root: Path | None = None
    seed: bool = False

    @property
    def self_updating(self) -> bool:
        """True for the per-user installs that manage their own versions."""
        return (
            self.kind in {InstallKind.WINDOWS, InstallKind.MACOS, InstallKind.LINUX}
            and self.root is not None
        )

    @property
    def state_path(self) -> Path | None:
        return None if self.root is None else self.root / STATE_FILE

    @property
    def versions_dir(self) -> Path | None:
        if self.root is None:
            return None
        return self.root / ("releases" if self.kind is InstallKind.LINUX else "versions")


@dataclass
class InstallState:
    """``R/state.json``. Unknown keys are kept so newer writers lose nothing."""

    active: str
    previous: str | None = None
    ready: dict[str, Any] | None = None
    pending_verification: bool = False
    unverified_launches: int = 0
    last_rollback: dict[str, Any] | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = dict(self.extra)
        data.update(
            {
                "schema": STATE_SCHEMA,
                "active": self.active,
                "previous": self.previous,
                "ready": self.ready,
                "pending_verification": self.pending_verification,
                "unverified_launches": self.unverified_launches,
                "last_rollback": self.last_rollback,
            }
        )
        return data

    @classmethod
    def from_dict(cls, data: object) -> InstallState | None:
        if not isinstance(data, dict) or data.get("schema") != STATE_SCHEMA:
            return None
        active = data.get("active")
        if not is_valid_version_name(active):
            return None
        previous = data.get("previous")
        ready = data.get("ready")
        if not (isinstance(ready, dict) and is_valid_version_name(ready.get("version"))):
            ready = None
        launches = data.get("unverified_launches")
        last_rollback = data.get("last_rollback")
        return cls(
            active=str(active),
            previous=str(previous) if is_valid_version_name(previous) else None,
            ready=dict(ready) if ready else None,
            pending_verification=data.get("pending_verification") is True,
            unverified_launches=(
                launches if isinstance(launches, int) and not isinstance(launches, bool) and launches > 0 else 0
            ),
            last_rollback=dict(last_rollback) if isinstance(last_rollback, dict) else None,
            extra={key: value for key, value in data.items() if key not in _KNOWN_STATE_KEYS},
        )


def is_valid_version_name(value: object) -> bool:
    """A version folder name: no separators, no traversal, at most 64 chars."""
    return isinstance(value, str) and bool(_VERSION_NAME.fullmatch(value)) and ".." not in value


def compare_versions(left: str, right: str) -> int:
    """Compare two release versions: -1, 0 or 1.

    ``5.2.10`` > ``5.2.9`` and a release beats its own pre-releases
    (``5.2.0`` > ``5.2.0-rc.1`` > ``5.2.0-beta.2``). Build metadata after ``+``
    is ignored.
    """

    def parse(value: str) -> tuple[tuple[int, ...], list[tuple[int, int | str]]]:
        text = value.strip().lower().lstrip("v").split("+", 1)[0]
        match = re.match(r"^(\d+(?:\.\d+)*)(.*)$", text)
        if not match:
            return (), [(1, text)]
        release = tuple(int(part) for part in match.group(1).split("."))
        suffix = [
            (0, int(part)) if part.isdigit() else (1, part)
            for part in re.split(r"[.\-_]+|(?<=\D)(?=\d)|(?<=\d)(?=\D)", match.group(2))
            if part
        ]
        return release, suffix

    left_release, left_suffix = parse(left)
    right_release, right_suffix = parse(right)
    width = max(len(left_release), len(right_release))
    left_padded = left_release + (0,) * (width - len(left_release))
    right_padded = right_release + (0,) * (width - len(right_release))
    if left_padded != right_padded:
        return -1 if left_padded < right_padded else 1
    if left_suffix == right_suffix:
        return 0
    if not left_suffix or not right_suffix:
        # The release itself is newer than any of its pre-releases.
        return 1 if not left_suffix else -1
    return -1 if left_suffix < right_suffix else 1


# ── Where the install lives ─────────────────────────────────────────────────


def default_install_root(
    system: str | None = None,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Path | None:
    """The per-user install root ``R`` for this operating system."""
    env = os.environ if environ is None else environ
    selected = (system or platform.system()).lower()
    user_home = home or Path.home()
    if selected == "windows":
        local = env.get("LOCALAPPDATA") or str(user_home / "AppData" / "Local")
        return Path(local) / "Programs" / WINDOWS_ROOT_NAME
    if selected == "darwin":
        return user_home / "Library" / "Application Support" / MACOS_ROOT_NAME
    if selected == "linux":
        data_home = env.get("XDG_DATA_HOME") or str(user_home / ".local" / "share")
        return Path(data_home) / LINUX_ROOT_NAME
    return None


def _default_app_root(environ: Mapping[str, str]) -> Path:
    configured = str(environ.get(APP_ROOT_ENV) or "").strip()
    if configured:
        return Path(configured).expanduser()
    # src/row_bot/install_layout.py -> the checkout or the installed app/ folder
    return Path(__file__).resolve().parents[2]


def _bundle_version(bundle_contents: Path) -> str | None:
    import plistlib

    try:
        with (bundle_contents / "Info.plist").open("rb") as handle:
            info = plistlib.load(handle)
    except (OSError, ValueError, plistlib.InvalidFileException):
        return None
    value = info.get("CFBundleShortVersionString") if isinstance(info, dict) else None
    return value if is_valid_version_name(value) else None


def detect(
    *,
    app_root: Path | None = None,
    environ: Mapping[str, str] | None = None,
    system: str | None = None,
    home: Path | None = None,
) -> InstallLayout:
    """Recognise the install this code runs from by where its app folder sits."""
    env = os.environ if environ is None else environ
    if env.get(CONTAINERIZED_ENV) == "1":
        return InstallLayout(InstallKind.CONTAINER)
    selected = (system or platform.system()).lower()
    app = Path(app_root) if app_root is not None else _default_app_root(env)
    try:
        app = app.resolve()
    except OSError:
        return InstallLayout(InstallKind.UNKNOWN)
    if (app / ".git").exists():
        return InstallLayout(InstallKind.DEV)
    version_root = app.parent
    if app.name != "app" or not (version_root / "python").is_dir():
        return InstallLayout(InstallKind.UNKNOWN)

    container = version_root.parent
    if selected == "darwin" and version_root.name == "Resources" and container.name == "Contents" \
            and container.parent.suffix == ".app":
        version = _bundle_version(container)
        configured = str(env.get(INSTALL_ROOT_ENV) or "").strip()
        root = Path(configured).expanduser() if configured else default_install_root("darwin", env, home)
        if version is None or root is None:
            return InstallLayout(InstallKind.UNKNOWN)
        return InstallLayout(InstallKind.MACOS, root, version, version_root, seed=True)

    expected_parent = {"windows": "versions", "darwin": "versions", "linux": "releases"}.get(selected)
    if expected_parent is None or container.name != expected_parent or not is_valid_version_name(version_root.name):
        return InstallLayout(InstallKind.UNKNOWN)
    kind = {"windows": InstallKind.WINDOWS, "darwin": InstallKind.MACOS, "linux": InstallKind.LINUX}[selected]
    return InstallLayout(kind, container.parent, version_root.name, version_root)


def version_root(layout: InstallLayout, version: str) -> Path | None:
    """The folder holding ``app/`` and ``python/`` for *version* in this install."""
    versions = layout.versions_dir
    if versions is None or not is_valid_version_name(version):
        return None
    return versions / version


# ── state.json ──────────────────────────────────────────────────────────────


def new_state(active: str) -> InstallState:
    return InstallState(active=active)


def read_state(root: Path) -> InstallState | None:
    """The install's state, or None when it is missing or not valid."""
    try:
        data = json.loads((Path(root) / STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return InstallState.from_dict(data)


def write_state(root: Path, state: InstallState, *, attempts: int = 40) -> None:
    """Write ``state.json`` atomically: a temporary file next to it, then a rename.

    On Windows a reader without delete sharing makes the rename fail for a
    moment, so it is retried briefly.
    """
    folder = Path(root)
    folder.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(state.to_dict(), indent=2, sort_keys=True) + "\n"
    handle, temporary = tempfile.mkstemp(prefix=".state.", suffix=".tmp", dir=folder)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        for attempt in range(attempts):
            try:
                os.replace(temporary, folder / STATE_FILE)
                return
            except PermissionError:
                if attempt == attempts - 1:
                    raise
                time.sleep(0.05)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def linux_current_version(root: Path) -> str | None:
    """The release the Linux ``current`` link points at."""
    link = Path(root) / LINUX_CURRENT_LINK
    try:
        target = os.readlink(link)
    except OSError:
        return None
    name = Path(target).name
    return name if is_valid_version_name(name) else None


def set_linux_current(root: Path, version: str) -> None:
    """Point ``current`` at ``releases/<version>`` with one atomic rename."""
    if not is_valid_version_name(version):
        raise ValueError(f"not a version folder name: {version!r}")
    folder = Path(root)
    temporary = folder / f".current.{os.getpid()}.{time.time_ns()}"
    os.symlink(f"releases/{version}", temporary, target_is_directory=True)
    try:
        os.replace(temporary, folder / LINUX_CURRENT_LINK)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def load_state(root: Path, kind: InstallKind) -> InstallState | None:
    """``state.json`` with the Linux ``current`` link as the active version."""
    state = read_state(root)
    if kind is not InstallKind.LINUX:
        return state
    current = linux_current_version(root)
    if current is None:
        return state
    if state is None:
        return new_state(current)
    if state.active != current:
        state.active = current
    return state


# ── The loop guard ──────────────────────────────────────────────────────────


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def begin_launch(
    state: InstallState,
    *,
    max_unverified: int = MAX_UNVERIFIED_LAUNCHES,
    now: Callable[[], str] = _now_iso,
) -> tuple[InstallState, str | None]:
    """The native launchers' rule, applied to every start of the app.

    While ``pending_verification`` is set (the version has not become ready
    since the switch), each launch adds one to ``unverified_launches``. Once
    ``max_unverified`` launches did not become ready, ``active`` goes back to
    ``previous``, the flag is cleared and ``last_rollback`` records it.
    Returns the new state and the version rolled back from, if any.
    """
    updated = copy.deepcopy(state)
    if not updated.pending_verification:
        updated.unverified_launches = 0
        return updated, None
    previous = updated.previous
    if updated.unverified_launches >= max_unverified and previous and previous != updated.active:
        failed = updated.active
        updated.active = previous
        updated.previous = None
        updated.pending_verification = False
        updated.unverified_launches = 0
        if updated.ready and updated.ready.get("version") == failed:
            updated.ready = None
        updated.last_rollback = {
            "from": failed,
            "to": previous,
            "at": now(),
            "reason": f"{max_unverified} launches in a row did not become ready",
        }
        return updated, failed
    updated.unverified_launches += 1
    return updated, None


def relaunch_after_exit(
    state: InstallState | None,
    launched: str,
    exit_code: int,
    *,
    timed_out: bool = False,
    max_unverified: int = MAX_UNVERIFIED_LAUNCHES,
) -> bool:
    """Whether the native launcher starts the app again after it exited.

    Always after :data:`RELAUNCH_EXIT_CODE`. After a failed start of a version
    that is still unverified (a crash or the readiness timeout) only while
    another launch can still succeed or roll back. A normal quit never
    relaunches.
    """
    if exit_code == RELAUNCH_EXIT_CODE:
        return True
    if state is None or not state.pending_verification or state.active != launched:
        return False
    if not timed_out and exit_code == 0:
        return False
    return bool(state.previous) or state.unverified_launches < max_unverified


def mark_verified(layout: InstallLayout) -> bool:
    """Clear ``pending_verification`` because the running version became ready."""
    if not layout.self_updating or layout.root is None or layout.version is None:
        return False
    state = load_state(layout.root, layout.kind)
    if state is None or state.active != layout.version:
        return False
    if not state.pending_verification and state.unverified_launches == 0:
        return False
    state.pending_verification = False
    state.unverified_launches = 0
    state.extra["verified_at"] = _now_iso()
    write_state(layout.root, state)
    return True


def _readyz_answers(port: int, timeout: float = 1.0) -> bool:
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{int(port)}/readyz", timeout=timeout) as response:  # noqa: S310 - loopback only
            return int(getattr(response, "status", 0)) == 200
    except (OSError, ValueError, urllib.error.URLError):
        return False


def verify_when_ready(
    layout: InstallLayout,
    port: int,
    *,
    timeout: float = READY_TIMEOUT_SECONDS,
    interval: float = 0.25,
    probe: Callable[[int], bool] = _readyz_answers,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Wait for this version's server to answer ``/readyz``, then verify it.

    Returns at once when the install has nothing waiting for verification.
    """
    if not layout.self_updating or layout.root is None or layout.version is None:
        return False
    state = load_state(layout.root, layout.kind)
    if state is None or state.active != layout.version:
        return False
    if not state.pending_verification and state.unverified_launches == 0:
        return False
    deadline = clock() + timeout
    while clock() < deadline:
        if probe(port):
            return mark_verified(layout)
        sleep(interval)
    return False


# ── Installers ──────────────────────────────────────────────────────────────


def record_install(root: Path, version: str, *, kind: InstallKind) -> InstallState:
    """State after a full installer put *version* in place and made it active.

    The version that was active before becomes ``previous`` (when it is still
    there); a staged update and an old verification are dropped.
    """
    if not is_valid_version_name(version):
        raise ValueError(f"not a version folder name: {version!r}")
    folder = Path(root)
    before = load_state(folder, kind)
    versions = folder / ("releases" if kind is InstallKind.LINUX else "versions")
    previous = None
    if before is not None:
        for candidate in (before.active, before.previous):
            if candidate and candidate != version and (versions / candidate).is_dir():
                previous = candidate
                break
    state = InstallState(active=version, previous=previous)
    if before is not None:
        state.extra = dict(before.extra)
        state.extra.pop("verified_at", None)
        state.last_rollback = before.last_rollback
    write_state(folder, state)
    return state


def prune_versions(root: Path, kind: InstallKind, keep: set[str]) -> list[str]:
    """Delete version folders other than *keep* (never ``current``'s target)."""
    folder = Path(root)
    versions = folder / ("releases" if kind is InstallKind.LINUX else "versions")
    protected = {name for name in keep if name}
    if kind is InstallKind.LINUX:
        current = linux_current_version(folder)
        if current:
            protected.add(current)
    removed: list[str] = []
    try:
        children = sorted(versions.iterdir())
    except OSError:
        return removed
    for child in children:
        if child.name in protected or child.is_symlink() or not child.is_dir():
            continue
        shutil.rmtree(child, ignore_errors=True)
        if not child.exists():
            removed.append(child.name)
    return removed


def version_metadata(version: str, *, python_version: str, lock_file: Path | None = None) -> dict[str, Any]:
    """The ``version.json`` a build writes next to a version's ``app/`` and ``python/``."""
    lock_hash = None
    if lock_file is not None:
        lock_hash = "sha256:" + hashlib.sha256(Path(lock_file).read_bytes()).hexdigest()
    return {
        "schema": 1,
        "version": version,
        "python": python_version,
        "lock_hash": lock_hash,
        "source": "installer",
    }


# ── Command line (Linux wrapper, install.sh, build scripts) ────────────────


def _cli_kind(value: str) -> InstallKind:
    return InstallKind(value)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Row-Bot install layout helper")
    commands = parser.add_subparsers(dest="command", required=True)

    begin = commands.add_parser("begin-launch", help="Apply the loop guard; print '<version> <pending 0|1>'")
    begin.add_argument("--root", type=Path, required=True)
    begin.add_argument("--kind", type=_cli_kind, required=True)

    after = commands.add_parser("after-exit", help="Exit 0 to start the app again, 1 to stop")
    after.add_argument("--root", type=Path, required=True)
    after.add_argument("--kind", type=_cli_kind, required=True)
    after.add_argument("--version", required=True)
    after.add_argument("--code", type=int, required=True)
    after.add_argument("--timed-out", action="store_true")

    unverified = commands.add_parser("still-unverified", help="Exit 0 while VERSION is active and unverified")
    unverified.add_argument("--root", type=Path, required=True)
    unverified.add_argument("--kind", type=_cli_kind, required=True)
    unverified.add_argument("--version", required=True)

    install = commands.add_parser("record-install", help="Make a freshly installed version active")
    install.add_argument("--root", type=Path, required=True)
    install.add_argument("--kind", type=_cli_kind, required=True)
    install.add_argument("--version", required=True)
    install.add_argument("--prune", action="store_true", help="Delete versions other than active and previous")

    metadata = commands.add_parser("version-json", help="Write a version.json for a build")
    metadata.add_argument("--version", required=True)
    metadata.add_argument("--python", required=True)
    metadata.add_argument("--lock", type=Path)
    metadata.add_argument("--out", type=Path, required=True)

    args = parser.parse_args(argv)
    if args.command == "begin-launch":
        state = load_state(args.root, args.kind)
        if state is None:
            return 1
        updated, failed = begin_launch(state)
        if failed and args.kind is InstallKind.LINUX:
            set_linux_current(args.root, updated.active)
        on_disk = read_state(args.root)
        if on_disk is None or on_disk.to_dict() != updated.to_dict():
            write_state(args.root, updated)
        if failed:
            print(f"rolled back from {failed} to {updated.active}", file=sys.stderr)
        print(f"{updated.active} {1 if updated.pending_verification else 0}")
        return 0
    if args.command == "after-exit":
        state = load_state(args.root, args.kind)
        return 0 if relaunch_after_exit(state, args.version, args.code, timed_out=args.timed_out) else 1
    if args.command == "still-unverified":
        state = load_state(args.root, args.kind)
        return 0 if state is not None and state.pending_verification and state.active == args.version else 1
    if args.command == "record-install":
        state = record_install(args.root, args.version, kind=args.kind)
        if args.kind is InstallKind.LINUX:
            set_linux_current(args.root, state.active)
        if args.prune:
            prune_versions(args.root, args.kind, {state.active, state.previous or ""})
        print(f"{state.active} {state.previous or '-'}")
        return 0
    if args.command == "version-json":
        data = version_metadata(args.version, python_version=args.python, lock_file=args.lock)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
