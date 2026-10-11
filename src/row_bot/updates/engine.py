"""The background update engine and its state machine.

Phases::

    idle ─► downloading ─► staging ─► ready ─► applying ─► (restart) ─► verified
      ▲          │             │        │
      └──────────┴── failed ◄──┘        └─ (quit) the next start runs it

``checking`` is reported by the updater while a release check runs. The phase,
the version and any failure are kept in ``R/.updates/engine.json`` so they
survive a restart; ``ready`` itself lives in the install's ``state.json``,
which is the source of truth the launchers read. Downloads stay in
``R/.updates/<version>/`` until the version is ready, so an interrupted
download resumes.

Everything with an outside effect (transport, stager, activity probes, the
relaunch request, the clock) is passed in, so the deterministic tests run the
whole machine against fakes.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import platform
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Literal, Mapping

from row_bot import install_layout
from row_bot.install_layout import InstallLayout
from row_bot.updates import activity as activity_module
from row_bot.updates import switcher
from row_bot.updates.downloader import (
    DownloadCancelled,
    DownloadError,
    Downloader,
    HttpsTransport,
    Transport,
)
from row_bot.updates.manifest import (
    ManifestError,
    assets_content_hash,
    parse_manifest,
    platform_key,
    read_version_metadata,
)
from row_bot.updates.planner import (
    RuntimePlan,
    installed_distributions,
    plan_parts,
    plan_runtime,
)
from row_bot.updates.stager import (
    StageCancelled,
    StageError,
    StageInputs,
    Stager,
    remove_tree,
    site_packages_relative,
)

logger = logging.getLogger(__name__)

Phase = Literal["idle", "checking", "downloading", "staging", "ready", "applying", "verified", "failed"]
ApplyWhen = Literal["now", "when_idle", "on_quit"]
STATE_FILE = "engine.json"
WORK_DIR = ".updates"
IDLE_POLL_SECONDS = 15.0
VERIFY_WAIT_SECONDS = install_layout.READY_TIMEOUT_SECONDS + 60.0

_MESSAGES = {
    "download": "The download stopped. Row-Bot will try again.",
    "integrity": "A downloaded file did not match the release. Nothing was changed.",
    "manifest": "This release's update description could not be read. Nothing was changed.",
    "full_installer": "This update needs the full installer.",
    "disk": "There isn't enough free disk space to prepare the update.",
    "rolled_back": "The new version didn't start, so Row-Bot went back to the one that works.",
    "busy": "Another Row-Bot on this computer is preparing the update.",
    "clone": "Preparing the update failed while copying the runtime.",
    "libraries": "Preparing the update failed while installing its libraries.",
    "unpack": "Preparing the update failed while unpacking it.",
    "compile": "Preparing the update failed while compiling it.",
    "self_test": "The new version failed its self-test, so it wasn't installed.",
    "prepare": "The update could not be prepared.",
    "finish": "The prepared update could not be put in place.",
}


class EngineError(RuntimeError):
    """A refused engine command; ``code`` maps to a client error code."""

    def __init__(self, code: str, detail: list[str] | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail or []


@dataclass(frozen=True)
class ReleaseFiles:
    """Where one release's files are, from the release check (GitHub or a feed)."""

    version: str
    manifest_url: str
    manifest_sha256: str
    manifest_size: int | None
    #: Release asset name -> (download URL, size or None).
    files: Mapping[str, tuple[str, int | None]] = field(default_factory=dict)
    page_url: str = ""
    installer_url: str = ""

    def url_for(self, name: str) -> str:
        entry = self.files.get(name)
        if entry is None:
            raise ManifestError(f"the release has no file named {name}")
        return entry[0]


@dataclass
class EngineStatus:
    phase: Phase = "idle"
    version: str | None = None
    step: str | None = None
    message: str | None = None
    error: str | None = None
    retryable: bool = False
    apply_when: ApplyWhen | None = None
    full_installer_url: str | None = None
    updated_at: str | None = None
    downloaded: int = 0
    total: int = 0

    def persisted(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("downloaded")
        data.pop("total")
        return {"schema": 1, **data}


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class _InstallLock:
    """One preparing Row-Bot per install, across processes."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle = None

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


def _current_lock_hash(version_root: Path) -> str | None:
    recorded = read_version_metadata(version_root).get("lock_hash")
    if isinstance(recorded, str) and recorded.startswith("sha256:"):
        return recorded.lower()
    lock = version_root / "app" / "uv.lock"
    if lock.is_file():
        import hashlib

        return "sha256:" + hashlib.sha256(lock.read_bytes()).hexdigest()
    return None


def _target_environment() -> tuple[dict[str, str], list[Any]]:
    from packaging.markers import default_environment
    from packaging.tags import sys_tags

    return dict(default_environment()), list(sys_tags())


class UpdateEngine:
    """Prepares, applies and tidies updates for one self-updating install."""

    def __init__(
        self,
        layout: InstallLayout,
        *,
        transport: Transport | None = None,
        stager_factory: Callable[..., Stager] | None = None,
        activity: Callable[[], list[str]] = activity_module.active_work,
        relaunch: Callable[[], bool] | None = None,
        environment: Callable[[], tuple[dict[str, str], list[Any]]] = _target_environment,
        python_version: Callable[[], str] = platform.python_version,
        platform_id: Callable[[], str | None] = platform_key,
        on_change: Callable[[], None] | None = None,
        background: bool = True,
        idle_poll: float = IDLE_POLL_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not layout.self_updating or layout.root is None or layout.version_root is None:
            raise EngineError("update_unavailable")
        self.layout = layout
        self.root = layout.root
        self.work_root = layout.root / WORK_DIR
        self._transport = transport
        self._stager_factory = stager_factory or Stager
        self._activity = activity
        self._relaunch = relaunch or _request_relaunch
        self._environment = environment
        self._python_version = python_version
        self._platform_id = platform_id
        self._on_change = on_change or (lambda: None)
        self._background = background
        self._sleep = sleep
        self._idle_poll = idle_poll
        self._lock = threading.RLock()
        self._cancel = threading.Event()
        self._worker: threading.Thread | None = None
        self._idle_watch: threading.Thread | None = None
        self._install_lock = _InstallLock(self.work_root / "engine.lock")
        self._status = self._recover()

    # ── persisted state ─────────────────────────────────────────────────

    @property
    def state_path(self) -> Path:
        return self.work_root / STATE_FILE

    def _load(self) -> EngineStatus:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return EngineStatus()
        if not isinstance(data, dict) or data.get("schema") != 1:
            return EngineStatus()
        status = EngineStatus()
        phase = data.get("phase")
        if phase in {"idle", "downloading", "staging", "ready", "applying", "verified", "failed"}:
            status.phase = phase
        version = data.get("version")
        status.version = version if install_layout.is_valid_version_name(version) else None
        for key in ("step", "message", "error", "updated_at", "full_installer_url"):
            value = data.get(key)
            setattr(status, key, str(value)[:512] if isinstance(value, str) else None)
        status.retryable = data.get("retryable") is True
        when = data.get("apply_when")
        status.apply_when = when if when in {"now", "when_idle", "on_quit"} else None
        return status

    def _save(self, status: EngineStatus) -> None:
        self.work_root.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(status.persisted(), indent=2, sort_keys=True) + "\n"
        handle, temporary = tempfile.mkstemp(prefix=".engine.", suffix=".tmp", dir=self.work_root)
        try:
            with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(payload)
            for attempt in range(20):
                try:
                    os.replace(temporary, self.state_path)
                    break
                except PermissionError:
                    if attempt == 19:
                        raise
                    time.sleep(0.05)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise

    def _set(self, **changes: Any) -> EngineStatus:
        with self._lock:
            self._status = replace(self._status, updated_at=_now_iso(), **changes)
            status = self._status
            try:
                self._save(status)
            except OSError:
                logger.debug("Could not save the update engine state", exc_info=True)
        try:
            self._on_change()
        except Exception:  # noqa: BLE001 - listeners never break the engine
            logger.debug("Update listener failed", exc_info=True)
        return status

    def _recover(self) -> EngineStatus:
        """Reconcile what was saved with ``state.json`` after a (re)start."""
        saved = self._load()
        state = install_layout.load_state(self.root, self.layout.kind)
        running = self.layout.version
        ready = switcher.ready_version(self.layout)
        status = replace(saved, downloaded=0, total=0)
        if saved.phase == "applying" and saved.version:
            if running == saved.version:
                status = EngineStatus("verified", saved.version, message=f"Row-Bot is now {saved.version}.")
            elif state is not None and state.last_rollback and state.last_rollback.get("from") == saved.version:
                status = EngineStatus(
                    "failed", saved.version, error="rolled_back", message=_MESSAGES["rolled_back"], retryable=False
                )
            elif state is not None and state.active == saved.version:
                status = replace(saved, message="Restart Row-Bot to finish updating.")
        elif saved.phase in {"downloading", "staging"}:
            # Interrupted: the downloads are kept and the next start resumes.
            if saved.version:
                staging = (self.layout.versions_dir or self.root) / f"{saved.version}.staging"
                remove_tree(staging)
            status = EngineStatus("idle", saved.version, message="Preparing the update was interrupted.")
        if ready and status.phase not in {"applying", "verified"}:
            status = replace(
                status,
                phase="ready",
                version=ready,
                error=None,
                retryable=False,
                message=None,
                apply_when=saved.apply_when if saved.phase == "ready" else None,
            )
        elif status.phase == "ready" and not ready:
            status = EngineStatus()
        status.updated_at = saved.updated_at or _now_iso()
        if status.phase == "ready" and status.apply_when == "when_idle":
            self._status = status
            self._start_idle_watch()
        return status

    # ── reading ─────────────────────────────────────────────────────────

    def status(self) -> EngineStatus:
        with self._lock:
            return replace(self._status)

    def busy(self) -> bool:
        """Preparing in this process, or switched and waiting for the restart."""
        with self._lock:
            preparing = self._worker is not None and self._worker.is_alive()
            return preparing or self._status.phase == "applying"

    def acknowledge(self) -> None:
        """A new check makes "verified" and an old failure history."""
        with self._lock:
            if self._status.phase == "verified":
                self._set(phase="idle", message=None)

    # ── preparing ───────────────────────────────────────────────────────

    def start(self, release: ReleaseFiles) -> bool:
        """Download and stage *release* in the background. False if already running."""
        if not install_layout.is_valid_version_name(release.version):
            raise EngineError("update_unavailable")
        if install_layout.compare_versions(release.version, self.layout.version or "0") <= 0:
            return False
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                return False
            if self._status.phase == "applying":
                return False
            if self._status.phase == "ready" and self._status.version == release.version:
                return False
            self._cancel.clear()
            self._set(
                phase="downloading", version=release.version, step="manifest", error=None,
                message=None, retryable=False, full_installer_url=None, apply_when=None,
                downloaded=0, total=0,
            )
            if self._background:
                self._worker = threading.Thread(
                    target=self._run, args=(release,), daemon=True, name="row-bot-update-engine"
                )
                self._worker.start()
                return True
        self._run(release)
        return True

    def cancel(self) -> bool:
        with self._lock:
            if self._status.phase not in {"downloading", "staging"}:
                return False
            self._cancel.set()
            self._status.message = "Stopping…"
        return True

    def wait(self, timeout: float | None = None) -> None:
        worker = self._worker
        if worker is not None:
            worker.join(timeout)

    def _progress(self, added: int) -> None:
        with self._lock:
            self._status.downloaded = max(0, self._status.downloaded + added)

    def _run(self, release: ReleaseFiles) -> None:
        if not self._install_lock.acquire():
            self._set(phase="failed", error="busy", message=_MESSAGES["busy"], retryable=True, step=None)
            return
        work = self.work_root / release.version
        keep_downloads = True
        try:
            transport = self._transport or HttpsTransport()
            downloader = Downloader(transport, cancel=self._cancel, sleep=self._sleep)
            work.mkdir(parents=True, exist_ok=True)
            manifest_path = downloader.fetch(
                release.manifest_url, work / "update.json",
                sha256=release.manifest_sha256, size=release.manifest_size,
            ).path
            manifest = parse_manifest(manifest_path.read_bytes(), expected_version=release.version)
            version_root = self.layout.version_root
            assert version_root is not None
            parts = plan_parts(
                manifest,
                current_version=self.layout.version or "",
                platform_key=self._platform_id(),
                current_python=self._python_version(),
                current_lock_hash=_current_lock_hash(version_root),
                current_assets_hash=assets_content_hash(version_root / "app"),
            )
            if parts.runtime is not None and parts.runtime.mode == "full":
                keep_downloads = False
                self._needs_full_installer(release, parts.runtime.reason)
                return
            files = [parts.code] + ([parts.assets] if parts.assets else []) + ([parts.pylock] if parts.pylock else [])
            with self._lock:
                self._status.total = sum(item.size for item in files)
                self._status.downloaded = 0
            self._set(step="code")
            fetched: dict[str, Path] = {}
            for item in files:
                fetched[item.file] = downloader.fetch(
                    release.url_for(item.file), work / item.file,
                    sha256=item.sha256, size=item.size, progress=self._progress,
                ).path
            runtime: RuntimePlan = parts.runtime or RuntimePlan("reuse", "")
            site = version_root / "python" / site_packages_relative(version_root / "python")
            installed = installed_distributions(site)
            if parts.pylock is not None:
                env, tags = self._environment()
                runtime = plan_runtime(
                    fetched[parts.pylock.file].read_text(encoding="utf-8"),
                    environment=env, tags=tags, installed=installed,
                )
                if runtime.mode == "full":
                    keep_downloads = False
                    self._needs_full_installer(release, runtime.reason)
                    return
            wheels: dict[str, Path] = {}
            if runtime.mode == "sync":
                self._set(step="libraries")
                with self._lock:
                    self._status.total += runtime.wheel_bytes
                for pick in runtime.wheels:
                    wheels[pick.name] = downloader.fetch(
                        pick.url, work / "wheels" / pick.filename,
                        sha256=pick.sha256, size=pick.size, progress=self._progress,
                    ).path
            self._set(phase="staging", step="clone")
            stager = self._stager_factory(
                self.layout, work_dir=work / "stage", cancel=self._cancel,
                on_step=lambda step: self._set(step=step),
            )
            stager.stage(
                StageInputs(
                    manifest=manifest,
                    runtime=runtime,
                    installed=installed,
                    code_archive=fetched[parts.code.file],
                    assets_archive=fetched.get(parts.assets.file) if parts.assets else None,
                    wheels=wheels,
                    python_version=self._python_version(),
                )
            )
            keep_downloads = False
            self._set(phase="ready", version=manifest.version, step=None, error=None, message=None, retryable=False)
            logger.info("Row-Bot %s is ready; it starts at the next restart", manifest.version)
        except (DownloadCancelled, StageCancelled):
            self._set(phase="idle", step=None, message="Update download cancelled.", error=None)
        except DownloadError as exc:
            keep_downloads = exc.retryable
            code = "download" if exc.retryable else "integrity"
            logger.warning("Update download failed: %s", exc)
            self._set(phase="failed", step=None, error=code, message=_MESSAGES[code], retryable=True)
        except ManifestError as exc:
            keep_downloads = False
            logger.warning("Update description rejected: %s", exc)
            self._set(phase="failed", step=None, error="manifest", message=_MESSAGES["manifest"], retryable=False)
        except StageError as exc:
            logger.warning("Preparing the update failed at %s: %s", exc.step, exc.message)
            code = exc.step if exc.step in _MESSAGES else "prepare"
            self._set(phase="failed", step=None, error=code, message=_MESSAGES[code], retryable=exc.retryable)
        except OSError as exc:
            if exc.errno == errno.ENOSPC:
                self._set(phase="failed", step=None, error="disk", message=_MESSAGES["disk"], retryable=True)
            else:
                logger.warning("Preparing the update failed", exc_info=True)
                self._set(phase="failed", step=None, error="prepare", message=_MESSAGES["prepare"], retryable=True)
        except Exception:  # noqa: BLE001 - the engine reports, it never crashes the app
            logger.exception("Preparing the update failed")
            self._set(phase="failed", step=None, error="prepare", message=_MESSAGES["prepare"], retryable=True)
        finally:
            if not keep_downloads:
                remove_tree(work)
            self._install_lock.release()

    def offer_full_installer(self, release: ReleaseFiles, reason: str) -> None:
        """The release cannot be prepared in the background (D15, D16)."""
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                return
            if self._status.phase in {"ready", "applying"}:
                return
            if self._status.version == release.version and self._status.error == "full_installer":
                return
        self._set(version=release.version)
        self._needs_full_installer(release, reason)

    def _needs_full_installer(self, release: ReleaseFiles, reason: str) -> None:
        logger.info("Update %s needs the full installer: %s", release.version, reason)
        url = release.installer_url or release.page_url or None
        self._set(
            phase="failed", step=None, error="full_installer", message=_MESSAGES["full_installer"],
            retryable=False, full_installer_url=url,
        )

    # ── applying ────────────────────────────────────────────────────────

    def apply(self, *, when: ApplyWhen, force: bool = False, expected: str | None = None) -> EngineStatus:
        version = switcher.ready_version(self.layout)
        if version is None:
            raise EngineError("update_not_ready")
        if expected is not None and expected != version:
            raise EngineError("update_changed")
        if when == "on_quit":
            return self._set(phase="ready", version=version, apply_when="on_quit")
        if when == "when_idle":
            status = self._set(phase="ready", version=version, apply_when="when_idle")
            self._start_idle_watch()
            return status
        work = self._activity()
        if work and not force:
            raise EngineError("update_restart_busy", work)
        return self._apply_now(version)

    def _apply_now(self, version: str) -> EngineStatus:
        if not self._install_lock.acquire():
            raise EngineError("update_busy")
        try:
            switcher.switch_to_ready(self.layout, expected=version)
        except switcher.SwitchError:
            raise EngineError("update_not_ready") from None
        finally:
            self._install_lock.release()
        status = self._set(phase="applying", version=version, apply_when="now", message="Restarting…")
        try:
            restarting = bool(self._relaunch())
        except Exception:  # noqa: BLE001
            logger.warning("Could not ask the launcher to restart", exc_info=True)
            restarting = False
        if not restarting:
            status = self._set(message="Restart Row-Bot to finish updating.")
        return status

    def _start_idle_watch(self) -> None:
        if not self._background:
            return
        with self._lock:
            if self._idle_watch is not None and self._idle_watch.is_alive():
                return
            self._idle_watch = threading.Thread(target=self._watch_idle, daemon=True, name="row-bot-update-idle")
            self._idle_watch.start()

    def _watch_idle(self) -> None:
        while True:
            time.sleep(self._idle_poll)
            if not self.apply_when_idle_once():
                return

    def apply_when_idle_once(self) -> bool:
        """One look for "when idle": apply if idle. Returns True to keep watching."""
        status = self.status()
        if status.phase != "ready" or status.apply_when != "when_idle":
            return False
        if self._activity():
            return True
        try:
            self._apply_now(status.version or "")
        except EngineError:
            logger.info("The update waiting for an idle moment could not be applied")
        return False

    def rollback(self) -> str:
        """Repair: go back to ``previous`` and restart."""
        if not self._install_lock.acquire():
            raise EngineError("update_busy")
        try:
            target = switcher.rollback(self.layout)
        except switcher.SwitchError:
            raise EngineError("update_no_previous") from None
        finally:
            self._install_lock.release()
        self._set(phase="applying", version=target, apply_when="now", message="Going back…", error=None)
        try:
            restarting = bool(self._relaunch())
        except Exception:  # noqa: BLE001
            restarting = False
        if not restarting:
            self._set(message="Restart Row-Bot to finish going back.")
        return target

    # ── tidying ─────────────────────────────────────────────────────────

    def cleanup(self) -> list[str]:
        """Remove versions and downloads nothing needs any more (a verified start)."""
        if not self._install_lock.acquire():
            return []
        try:
            removed = switcher.cleanup_old_versions(self.layout)
            status = self.status()
            keep = {status.version} if status.phase in {"downloading", "staging", "failed", "idle"} else set()
            try:
                children = list(self.work_root.iterdir()) if self.work_root.is_dir() else []
            except OSError:
                children = []
            for child in children:
                if child.is_dir() and child.name not in keep:
                    remove_tree(child)
            return removed
        finally:
            self._install_lock.release()

    def cleanup_after_verified_start(self, *, wait: float = VERIFY_WAIT_SECONDS, poll: float = 5.0) -> list[str]:
        """Wait until this start is verified, then tidy (run in a background thread)."""
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            state = install_layout.load_state(self.root, self.layout.kind)
            if state is None or state.active != self.layout.version:
                return []
            if not state.pending_verification:
                return self.cleanup()
            time.sleep(poll)
        return []


def _request_relaunch() -> bool:
    from row_bot.access.launcher_control import request_launcher_relaunch

    return request_launcher_relaunch().accepted


__all__ = [
    "ApplyWhen",
    "EngineError",
    "EngineStatus",
    "Phase",
    "ReleaseFiles",
    "UpdateEngine",
]
