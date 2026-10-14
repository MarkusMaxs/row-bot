"""Build a new version next to the running one, test it, and mark it ready.

``versions/<v>.staging`` is assembled from:

1. a clone of the running version's ``python/`` (APFS ``clonefile`` on macOS,
   hard links on Windows and Linux, a copy when neither works), leaving out
   the libraries the update changes or removes ("prune");
2. the changed and added wheels, installed by the clone's own pip from local
   files only: ``pip install --no-index --no-deps --require-hashes`` (D14);
3. the code archive and, when they changed, the assets (otherwise the running
   version's ``static/`` and ``sounds/`` are cloned); then the staged
   version's own ``payload_trim.py trim`` removes what pip brought back that
   the installers leave out (reinstalled libraries' tests and launchers);
4. deterministic, path-independent bytecode (``compileall -s <root>
   --invalidation-mode unchecked-hash``), like the installers ship;
5. two self-tests with the staged Python, isolated from the running app:
   ``scripts/verify_runtime_dependencies.py all`` and ``import row_bot.server``.

Then ``version.json`` is written, the folder is renamed to ``versions/<v>``
and ``ready`` is set in ``state.json``. On any failure the staging folder is
removed and ``state.json`` is left exactly as it was.

Files shared with the running version through hard links are never written in
place: pruned libraries are absent from the clone, pip unlinks before it
writes, and ``compileall`` writes through a temporary file and a rename.
"""

from __future__ import annotations

import ctypes
import json
import logging
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping, Protocol, Sequence

from row_bot import install_layout
from row_bot.updates.manifest import ASSET_DIRS, UpdateManifest
from row_bot.updates.planner import InstalledDist, RuntimePlan, WheelPick, prune_paths

logger = logging.getLogger(__name__)

#: Linux releases carry these next to ``app/`` and ``python/``.
LINUX_SHELL_ITEMS = ("bin", "share", "install.sh", "uninstall.sh")
#: …and a new code archive may bring newer copies of these from ``installer/linux``.
LINUX_SHELL_SOURCES = {"row-bot": "bin/row-bot", "install.sh": "install.sh", "uninstall.sh": "uninstall.sh"}
SELF_TEST_TIMEOUT = 900.0
PIP_TIMEOUT = 1800.0
COMPILE_TIMEOUT = 900.0
TRIM_TIMEOUT = 600.0
_SCRUBBED_PREFIXES = ("PYTHON", "ROW_BOT_", "VIRTUAL_ENV", "UV_", "PIP_", "CONDA", "__PYVENV")

SERVER_IMPORT = (
    "import os, sys; app = os.getcwd(); sys.path[:0] = [os.path.join(app, 'src'), app]; "
    "import row_bot.server, row_bot.version; v = row_bot.version.__version__; "
    "print('row_bot.server imported, version', v); sys.exit(0 if v == sys.argv[1] else 3)"
)


class StageError(RuntimeError):
    """Preparing the new version failed at *step*."""

    def __init__(self, step: str, message: str, *, retryable: bool = True) -> None:
        super().__init__(f"{step}: {message}")
        self.step = step
        self.message = message
        self.retryable = retryable


class StageCancelled(RuntimeError):
    """Staging was cancelled; nothing was changed."""


class CommandRunner(Protocol):
    def __call__(
        self,
        command: Sequence[str],
        *,
        env: Mapping[str, str],
        cwd: Path | None,
        timeout: float,
        log: Path,
    ) -> int: ...


class _ProcessTree:
    """Everything a staging command starts, stopped together.

    POSIX: the command leads its own process group. Windows: it runs in a
    kill-on-close job, so its children also end if Row-Bot itself dies.
    """

    def __init__(self, process: subprocess.Popen) -> None:
        self.process = process
        self._job = None
        if os.name == "nt":
            try:
                from row_bot.plugins.worker_ownership import WindowsJob

                self._job = WindowsJob(process)
            except Exception:  # noqa: BLE001 - fall back to killing the one process
                logger.debug("No job object for a staging command", exc_info=True)

    def kill(self) -> None:
        if self._job is not None:
            self._job.close()
            self._job = None
        elif os.name != "nt":
            import signal

            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        if self.process.poll() is None:
            self.process.kill()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass

    def release(self) -> None:
        if self._job is not None:
            self._job.close()  # also ends anything the command left behind
            self._job = None


def run_command(
    command: Sequence[str],
    *,
    env: Mapping[str, str],
    cwd: Path | None,
    timeout: float,
    log: Path,
    cancel: threading.Event | None = None,
) -> int:
    """Run a command with output appended to *log*; stop it on timeout or cancel."""
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("ab") as handle:
        handle.write(("\n$ " + " ".join(str(part) for part in command) + "\n").encode("utf-8", "replace"))
        handle.flush()
        process = subprocess.Popen(  # noqa: S603 - the staged interpreter with fixed arguments
            [str(part) for part in command],
            stdin=subprocess.DEVNULL,
            stdout=handle,
            stderr=subprocess.STDOUT,
            env=dict(env),
            cwd=str(cwd) if cwd else None,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            start_new_session=os.name != "nt",
        )
        tree = _ProcessTree(process)
        deadline = time.monotonic() + timeout
        try:
            while True:
                try:
                    return process.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    pass
                if (cancel is not None and cancel.is_set()) or time.monotonic() > deadline:
                    tree.kill()
                    if cancel is not None and cancel.is_set():
                        raise StageCancelled(" ".join(str(part) for part in command[:3]))
                    handle.write(b"\n[timed out]\n")
                    return -9
        finally:
            tree.release()


def python_executable(version_root: Path, system: str | None = None) -> Path:
    if (system or sys.platform).lower().startswith("win"):
        return Path(version_root) / "python" / "python.exe"
    return Path(version_root) / "python" / "bin" / "python3"


def site_packages_relative(python_root: Path, system: str | None = None) -> str:
    """``Lib/site-packages`` (Windows) or ``lib/python3.X/site-packages``."""
    if (system or sys.platform).lower().startswith("win"):
        return "Lib/site-packages"
    found = sorted(Path(python_root).glob("lib/python3.*/site-packages"))
    if not found:
        raise StageError("plan", f"no site-packages under {python_root}", retryable=False)
    return found[0].relative_to(python_root).as_posix()


def runtime_environment(version_root: Path, *, data_dir: Path | None = None) -> dict[str, str]:
    """The environment the native launchers give a version, scrubbed of ours."""
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith(_SCRUBBED_PREFIXES)}
    env.update({"PYTHONNOUSERSITE": "1", "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"})
    python = Path(version_root) / "python"
    if os.name == "nt":
        env["TCL_LIBRARY"] = str(python / "tcl" / "tcl8.6")
        env["TK_LIBRARY"] = str(python / "tcl" / "tk8.6")
        env["PATH"] = os.pathsep.join([str(python / "Scripts"), str(python), env.get("PATH", "")])
    if (python / "playwright-browsers").is_dir():
        env["PLAYWRIGHT_BROWSERS_PATH"] = str(python / "playwright-browsers")
    if data_dir is not None:
        env["ROW_BOT_DATA_DIR"] = str(data_dir)
    return env


# ── Trees ───────────────────────────────────────────────────────────────────


def remove_tree(path: Path, *, attempts: int = 20, sleep: Callable[[float], None] = time.sleep) -> bool:
    """Remove a folder, clearing read-only bits and retrying briefly on Windows."""
    path = Path(path)

    def onerror(function, target, _exc):  # noqa: ANN001
        try:
            os.chmod(target, stat.S_IWRITE)
            function(target)
        except OSError:
            pass

    for attempt in range(attempts):
        if not path.exists() and not path.is_symlink():
            return True
        if path.is_symlink() or path.is_file():
            try:
                path.unlink()
            except OSError:
                pass
        else:
            if sys.version_info >= (3, 12):
                shutil.rmtree(path, onexc=onerror)
            else:  # pragma: no cover - the shipped runtimes are 3.12+
                shutil.rmtree(path, onerror=onerror)
        if not path.exists():
            return True
        sleep(min(1.0, 0.1 * (attempt + 1)))
    return not path.exists()


def _clonefile(source: Path, target: Path) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    function = libc.clonefile
    function.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint32]
    function.restype = ctypes.c_int
    if function(os.fsencode(source), os.fsencode(target), 0) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(target))


def default_clone_method(system: str | None = None) -> str:
    selected = (system or sys.platform).lower()
    return "clonefile" if selected == "darwin" else "hardlink"


def _skipped(relative: str, skip: set[str], prefixes: tuple[str, ...]) -> bool:
    return relative in skip or relative.startswith(prefixes)


def clone_tree(
    source: Path,
    target: Path,
    *,
    method: str,
    skip: set[str] | None = None,
) -> str:
    """Clone *source* to *target*; return the method that worked.

    ``skip`` holds ``/``-separated paths relative to *source*; an entry ending
    in ``/`` leaves out a whole folder. ``clonefile`` copies the folder with one
    call and then deletes the skipped entries from the copy (copy-on-write: the
    source is untouched). Hard links and ``clonefile`` fall back to a copy.
    """
    source, target = Path(source), Path(target)
    if target.exists():
        raise StageError("clone", f"{target.name} already exists", retryable=True)
    skip = set(skip or ())
    prefixes = tuple(entry for entry in skip if entry.endswith("/"))
    target.parent.mkdir(parents=True, exist_ok=True)
    if method == "clonefile":
        try:
            _clonefile(source, target)
        except (OSError, AttributeError) as exc:
            logger.info("clonefile unavailable (%s); copying instead", exc)
            remove_tree(target)
            return clone_tree(source, target, method="copy", skip=skip)
        for entry in sorted(skip, key=len, reverse=True):
            candidate = target / entry.rstrip("/")
            if candidate.is_dir() and not candidate.is_symlink():
                remove_tree(candidate)
            else:
                candidate.unlink(missing_ok=True)
        return "clonefile"
    link = method == "hardlink"
    for folder, dirs, names in os.walk(source):
        relative_folder = Path(folder).relative_to(source)
        relative_text = "" if str(relative_folder) == "." else relative_folder.as_posix() + "/"
        destination = target / relative_folder
        destination.mkdir(parents=True, exist_ok=True)
        for name in list(dirs):
            full = Path(folder) / name
            relative = f"{relative_text}{name}"
            if _skipped(relative + "/", skip, prefixes):
                dirs.remove(name)
                continue
            if full.is_symlink():
                os.symlink(os.readlink(full), destination / name, target_is_directory=True)
                dirs.remove(name)
        for name in names:
            full = Path(folder) / name
            relative = f"{relative_text}{name}"
            if _skipped(relative, skip, prefixes):
                continue
            if full.is_symlink():
                os.symlink(os.readlink(full), destination / name)
                continue
            if link:
                try:
                    os.link(full, destination / name)
                    continue
                except OSError as exc:
                    logger.info("Hard links unavailable (%s); copying instead", exc)
                    link = False
            shutil.copy2(full, destination / name)
    return "hardlink" if link else "copy"


def _remove_empty_parents(root: Path, entries: set[str]) -> None:
    folders = {(root / entry.rstrip("/")).parent for entry in entries}
    for folder in sorted(folders, key=lambda path: len(path.parts), reverse=True):
        current = folder
        while current != root and root in current.parents:
            try:
                current.rmdir()
            except OSError:
                break
            current = current.parent


MAX_ARCHIVE_MEMBERS = 50_000
MAX_ARCHIVE_BYTES = 2 * 1024**3


def _checked_members(tar: tarfile.TarFile) -> list[tarfile.TarInfo]:
    """Only plain files and folders with clean relative names, within limits.

    ``extractall(filter="data")`` is the second line: older runtimes (3.13.2 on
    Windows) carry tarfile filter bypasses fixed later, so nothing that could
    use one gets that far.
    """
    members: list[tarfile.TarInfo] = []
    total = 0
    for member in tar:
        if len(members) >= MAX_ARCHIVE_MEMBERS:
            raise StageError("unpack", "the archive has too many entries", retryable=False)
        name = member.name
        if not (member.isreg() or member.isdir()):
            raise StageError("unpack", f"the archive holds a link or special file: {name!r}", retryable=False)
        if member.isdir() and name in {".", "./"}:
            continue  # the archive's own root
        # "./app/x" (tar -C dir .) is the same as "app/x".
        clean = name[2:] if name.startswith("./") else name
        parts = clean.replace("\\", "/").split("/")
        if (
            not clean
            or "\\" in name
            or "\0" in name
            or name.startswith("/")
            or ":" in name
            or any(part in {"..", "."} for part in parts)
            or any(not part for part in parts[:-1])
        ):
            raise StageError("unpack", f"the archive holds an unsafe path: {name!r}", retryable=False)
        total += max(0, member.size)
        if total > MAX_ARCHIVE_BYTES:
            raise StageError("unpack", "the archive is too large", retryable=False)
        members.append(member)
    return members


def unpack(archive: Path, destination: Path) -> int:
    """Extract a ``tar.xz``/``tar.gz`` part: checked members, then ``filter="data"``."""
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:*") as tar:
        members = _checked_members(tar)
        tar.extractall(destination, members=members, filter="data")
    return len(members)


# ── Staging ─────────────────────────────────────────────────────────────────


@dataclass
class StageInputs:
    """What the engine downloaded and planned for one version."""

    manifest: UpdateManifest
    runtime: RuntimePlan
    installed: Mapping[str, InstalledDist]
    code_archive: Path
    assets_archive: Path | None
    wheels: Mapping[str, Path] = field(default_factory=dict)
    python_version: str = ""


@dataclass
class StageResult:
    version: str
    clone_method: str
    seconds: dict[str, float] = field(default_factory=dict)


class Stager:
    """Builds ``versions/<v>`` for one install. Every outside effect is a seam."""

    def __init__(
        self,
        layout: install_layout.InstallLayout,
        *,
        work_dir: Path,
        runner: CommandRunner | None = None,
        cancel: threading.Event | None = None,
        clone_method: str | None = None,
        system: str | None = None,
        on_step: Callable[[str], None] | None = None,
        now: Callable[[], str] | None = None,
    ) -> None:
        if not layout.self_updating or layout.root is None or layout.version_root is None:
            raise StageError("prepare", "this install does not update itself", retryable=False)
        self.layout = layout
        self.work_dir = Path(work_dir)
        self.cancel = cancel or threading.Event()
        self.system = system or sys.platform
        self.clone_method = clone_method or default_clone_method(self.system)
        self._on_step = on_step or (lambda _step: None)
        self._now = now or (lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        self._runner = runner or (
            lambda command, *, env, cwd, timeout, log: run_command(
                command, env=env, cwd=cwd, timeout=timeout, log=log, cancel=self.cancel
            )
        )
        self.log = self.work_dir / "staging.log"

    # The versions folder: R/versions or, on Linux, R/releases.
    @property
    def versions_dir(self) -> Path:
        assert self.layout.versions_dir is not None
        return self.layout.versions_dir

    def _check(self) -> None:
        if self.cancel.is_set():
            raise StageCancelled("cancelled")

    def _protected(self) -> set[str]:
        state = install_layout.load_state(self.layout.root, self.layout.kind)  # type: ignore[arg-type]
        names = {self.layout.version or ""}
        if state is not None:
            names.update({state.active, state.previous or ""})
        return {name for name in names if name}

    def stage(self, inputs: StageInputs) -> StageResult:
        version = inputs.manifest.version
        if not install_layout.is_valid_version_name(version):
            raise StageError("prepare", "not a version folder name", retryable=False)
        if version in self._protected():
            raise StageError("prepare", f"{version} is already installed", retryable=False)
        staging = self.versions_dir / f"{version}.staging"
        final = self.versions_dir / version
        seconds: dict[str, float] = {}
        result = StageResult(version, self.clone_method, seconds)
        try:
            for leftover in (staging, final):
                if leftover.exists() and not remove_tree(leftover):
                    raise StageError("prepare", f"could not remove an old {leftover.name}")
            self.work_dir.mkdir(parents=True, exist_ok=True)
            staging.mkdir(parents=True)
            started = time.monotonic()
            self._on_step("clone")
            result.clone_method = self._clone_runtime(inputs, staging)
            seconds["clone"] = round(time.monotonic() - started, 2)
            self._check()

            started = time.monotonic()
            self._on_step("libraries")
            self._install_wheels(inputs, staging)
            seconds["libraries"] = round(time.monotonic() - started, 2)
            self._check()

            started = time.monotonic()
            self._on_step("unpack")
            self._unpack_app(inputs, staging)
            seconds["unpack"] = round(time.monotonic() - started, 2)
            self._check()

            started = time.monotonic()
            self._trim_runtime(inputs, staging)
            seconds["trim"] = round(time.monotonic() - started, 2)
            self._check()

            started = time.monotonic()
            self._on_step("compile")
            self._compile(inputs, staging)
            seconds["compile"] = round(time.monotonic() - started, 2)
            self._check()

            started = time.monotonic()
            self._on_step("self_test")
            self._self_test(version, staging)
            seconds["self_test"] = round(time.monotonic() - started, 2)
            self._check()

            started = time.monotonic()
            self._on_step("finish")
            self._finish(inputs, staging, final, result)
            seconds["finish"] = round(time.monotonic() - started, 2)
            return result
        except BaseException:
            # Nothing before the last step writes state.json, and that step
            # removes the renamed folder again if it cannot set ``ready``.
            remove_tree(staging)
            raise

    # ── steps ──

    def _clone_runtime(self, inputs: StageInputs, staging: Path) -> str:
        source = self.layout.version_root / "python"  # type: ignore[operator]
        if not source.is_dir():
            raise StageError("clone", "the running version has no python folder", retryable=False)
        skip: set[str] = set()
        if inputs.runtime.mode == "sync":
            skip = prune_paths(
                inputs.runtime,
                inputs.installed,
                site_packages=site_packages_relative(source, self.system),
            )
        try:
            method = clone_tree(source, staging / "python", method=self.clone_method, skip=skip)
        except OSError as exc:
            raise StageError("clone", str(exc)) from exc
        if skip:
            _remove_empty_parents(staging / "python", skip)
        return method

    def _install_wheels(self, inputs: StageInputs, staging: Path) -> None:
        wheels: list[WheelPick] = inputs.runtime.wheels if inputs.runtime.mode == "sync" else []
        if not wheels:
            return
        paths: list[tuple[WheelPick, Path]] = []
        for pick in wheels:
            path = inputs.wheels.get(pick.name)
            if path is None or not Path(path).is_file():
                raise StageError("libraries", f"{pick.filename} was not downloaded")
            paths.append((pick, Path(path).resolve()))
        # pip reads each line as a requirement: an absolute path with " #" in
        # it (a Windows user name) would be cut at the comment. Paths relative
        # to pip's working folder hold only the wheels' own (checked) names.
        wheel_folder = Path(os.path.commonpath([str(path.parent) for _pick, path in paths]))
        lines = [
            f"./{path.relative_to(wheel_folder).as_posix()} --hash=sha256:{pick.sha256}"
            for pick, path in paths
        ]
        requirements = self.work_dir / "wheels.txt"
        requirements.write_text("\n".join(lines) + "\n", encoding="utf-8")
        python = python_executable(staging, self.system)
        env = runtime_environment(staging)
        env.update({"PIP_CONFIG_FILE": os.devnull, "PIP_DISABLE_PIP_VERSION_CHECK": "1", "PIP_NO_INPUT": "1"})
        code = self._runner(
            [
                str(python), "-m", "pip", "install",
                "--no-index", "--no-deps", "--require-hashes", "--only-binary", ":all:",
                "--no-compile", "--no-cache-dir", "--no-warn-script-location",
                "--disable-pip-version-check", "--break-system-packages",
                "-r", str(requirements),
            ],
            env=env, cwd=wheel_folder, timeout=PIP_TIMEOUT, log=self.log,
        )
        if code != 0:
            raise StageError("libraries", f"pip install failed (exit {code})")
        from row_bot.updates.planner import canonical_name, installed_distributions

        after = installed_distributions(
            staging / "python" / site_packages_relative(staging / "python", self.system)
        )
        wrong = [
            pick.name
            for pick in wheels
            if after.get(canonical_name(pick.name)) is None
            or after[canonical_name(pick.name)].version != pick.version_to
        ]
        left = [dist.name for dist in inputs.runtime.remove if canonical_name(dist.name) in after]
        if wrong or left:
            raise StageError("libraries", f"the runtime does not match the lock: {wrong + left}")

    def _unpack_app(self, inputs: StageInputs, staging: Path) -> None:
        app = staging / "app"
        try:
            unpack(inputs.code_archive, app)
            if inputs.assets_archive is not None:
                unpack(inputs.assets_archive, app)
        except (tarfile.TarError, OSError) as exc:
            raise StageError("unpack", str(exc)) from exc
        if not (app / "launcher.py").is_file() or not (app / "src" / "row_bot").is_dir():
            raise StageError("unpack", "the code archive has no launcher.py or src/row_bot", retryable=False)
        if inputs.assets_archive is None:
            current_app = self.layout.version_root / "app"  # type: ignore[operator]
            for name in ASSET_DIRS:
                if (current_app / name).is_dir() and not (app / name).exists():
                    try:
                        clone_tree(current_app / name, app / name, method=self.clone_method)
                    except OSError as exc:
                        raise StageError("unpack", f"could not reuse {name}: {exc}") from exc
        if self.layout.kind is install_layout.InstallKind.LINUX:
            self._linux_shell(staging, inputs.manifest.version)

    def _linux_shell(self, staging: Path, version: str) -> None:
        current = self.layout.version_root
        assert current is not None
        for name in LINUX_SHELL_ITEMS:
            source = current / name
            if source.is_dir():
                shutil.copytree(source, staging / name, symlinks=True)
            elif source.is_file():
                shutil.copy2(source, staging / name)
        bundled = staging / "app" / "installer" / "linux"
        for source_name, target in LINUX_SHELL_SOURCES.items():
            source = bundled / source_name
            if source.is_file():
                (staging / target).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, staging / target)
                os.chmod(staging / target, 0o755)
        info = current / "install_info.json"
        if info.is_file():
            try:
                data = json.loads(info.read_text(encoding="utf-8"))
            except ValueError:
                data = {}
            if isinstance(data, dict):
                data["version"] = version
                (staging / "install_info.json").write_text(
                    json.dumps(data, indent=2) + "\n", encoding="utf-8", newline="\n"
                )

    def _trim_runtime(self, inputs: StageInputs, staging: Path) -> None:
        """Trim the libraries pip reinstalled with the staged version's own rules.

        pip brings back a reinstalled library's test folders and console-script
        launchers, which the installers leave out (``payload_trim.json``). The
        rest of the clone was trimmed when it was built, so only pip's new files
        go; unlinking them never touches the running version (a hard link loses
        its staging entry, a clonefile copy is the staging's own).
        """
        if inputs.runtime.mode != "sync" or not inputs.runtime.wheels:
            return
        script = staging / "app" / "src" / "row_bot" / "payload_trim.py"
        if not script.is_file():
            return  # a version from before the shared rules
        python = python_executable(staging, self.system)
        # -I ignores PYTHONDONTWRITEBYTECODE, and nothing may write bytecode
        # into a version folder outside the compile step: -B.
        code = self._runner(
            [str(python), "-B", "-I", "-S", str(script), "trim", "--python-root", str(staging / "python")],
            env=runtime_environment(staging), cwd=staging, timeout=TRIM_TIMEOUT, log=self.log,
        )
        if code != 0:
            # Leftover test folders cost space, not correctness; the self-test
            # still checks the runtime.
            logger.warning("Trimming the reinstalled libraries failed (exit %s); keeping them as installed", code)

    def _compile(self, inputs: StageInputs, staging: Path) -> None:
        python = python_executable(staging, self.system)
        env = runtime_environment(staging)
        env.pop("PYTHONDONTWRITEBYTECODE", None)
        base = [str(python), "-m", "compileall", "-q", "-j", "0", "-s", str(staging),
                "--invalidation-mode", "unchecked-hash"]
        code = self._runner([*base, str(staging / "app")], env=env, cwd=staging, timeout=COMPILE_TIMEOUT, log=self.log)
        if code != 0:
            raise StageError("compile", f"compiling the new code failed (exit {code})")
        if inputs.runtime.mode != "sync" or not inputs.runtime.wheels:
            return
        from row_bot.updates.planner import canonical_name, installed_distributions

        site = staging / "python" / site_packages_relative(staging / "python", self.system)
        after = installed_distributions(site)
        files: list[str] = []
        for pick in inputs.runtime.wheels:
            dist = after.get(canonical_name(pick.name))
            if dist is None:
                continue
            for entry in dist.files:
                if entry.endswith(".py"):
                    candidate = (site / entry).resolve()
                    if candidate.is_file():
                        files.append(str(candidate))
        if not files:
            return
        listing = self.work_dir / "compile-libraries.txt"
        listing.write_text("\n".join(files) + "\n", encoding="utf-8")
        # Libraries compile leniently, as the installers do: a test fixture with
        # old syntax must not stop an update.
        self._runner([*base, "-i", str(listing)], env=env, cwd=staging, timeout=COMPILE_TIMEOUT, log=self.log)

    def _self_test(self, version: str, staging: Path) -> None:
        python = python_executable(staging, self.system)
        app = staging / "app"
        scratch = self.work_dir / "self-test-data"
        remove_tree(scratch)
        scratch.mkdir(parents=True, exist_ok=True)
        env = runtime_environment(staging, data_dir=scratch)
        checks = (
            ("dependencies", [str(python), str(app / "scripts" / "verify_runtime_dependencies.py"), "all"]),
            ("server_import", [str(python), "-c", SERVER_IMPORT, version]),
        )
        try:
            for label, command in checks:
                code = self._runner(command, env=env, cwd=app, timeout=SELF_TEST_TIMEOUT, log=self.log)
                if code != 0:
                    raise StageError("self_test", f"{label} failed (exit {code})")
        finally:
            remove_tree(scratch)

    def _finish(self, inputs: StageInputs, staging: Path, final: Path, result: StageResult) -> None:
        manifest = inputs.manifest
        metadata = {
            "schema": 1,
            "version": manifest.version,
            "python": inputs.python_version,
            "lock_hash": manifest.lock_hash,
            "source": "update",
            "parts": {
                "code": manifest.code.sha256,
                "assets": manifest.assets.sha256 if inputs.assets_archive is not None else None,
                "assets_content": manifest.assets.content_sha256,
            },
            "runtime": inputs.runtime.mode,
            "clone": result.clone_method,
            "staged_at": self._now(),
        }
        (staging / install_layout.VERSION_FILE).write_text(
            json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
        )
        for attempt in range(40):
            try:
                os.replace(staging, final)
                break
            except PermissionError:
                if attempt == 39:
                    raise StageError("finish", "the staged folder could not be renamed") from None
                time.sleep(0.25)
        try:
            mark_ready(self.layout, manifest.version, staged_at=metadata["staged_at"])
        except BaseException:
            remove_tree(final)
            raise


def mark_ready(layout: install_layout.InstallLayout, version: str, *, staged_at: str) -> None:
    """Set ``ready`` in ``state.json`` (and nothing else)."""
    assert layout.root is not None
    state = install_layout.load_state(layout.root, layout.kind)
    if state is None:
        if layout.version is None:
            raise StageError("finish", "the install has no state", retryable=False)
        state = install_layout.new_state(layout.version)
    state.ready = {"version": version, "staged_at": staged_at}
    install_layout.write_state(layout.root, state)


__all__ = [
    "CommandRunner",
    "StageCancelled",
    "StageError",
    "StageInputs",
    "StageResult",
    "Stager",
    "clone_tree",
    "default_clone_method",
    "mark_ready",
    "python_executable",
    "remove_tree",
    "run_command",
    "runtime_environment",
    "site_packages_relative",
    "unpack",
]
