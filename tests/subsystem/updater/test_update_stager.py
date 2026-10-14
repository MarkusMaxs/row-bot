"""Staging defences: hostile archives, pip's command line, and stopping its commands."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import tarfile
import threading
import time
import zipfile
from pathlib import Path

import pytest

from row_bot.install_layout import InstallLayout
from row_bot.updates import stager as stager_module
from row_bot.updates.engine import UpdateEngine
from row_bot.updates.stager import StageCancelled, StageError, Stager, clone_tree, run_command, unpack
from tests.fixtures.updates import (
    FakeRunner,
    FakeTransport,
    Release,
    add_dist,
    make_install,
    make_release,
    serve,
    sha256,
    site_packages,
    tree_fingerprint,
)

pytestmark = [pytest.mark.subsystem, pytest.mark.platform]

ROW_BOT_SOURCE = Path(__file__).resolve().parents[3] / "src" / "row_bot"


def _tar(path: Path, members: list[tuple[tarfile.TarInfo, bytes | None]]) -> Path:
    with tarfile.open(path, "w:gz") as tar:
        for info, data in members:
            if data is not None:
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
            else:
                tar.addfile(info)
    return path


def _file(name: str, data: bytes = b"x") -> tuple[tarfile.TarInfo, bytes]:
    return tarfile.TarInfo(name), data


def _special(name: str, kind: bytes, link: str = "") -> tuple[tarfile.TarInfo, None]:
    info = tarfile.TarInfo(name)
    info.type = kind
    info.linkname = link
    return info, None


@pytest.mark.parametrize(
    "member",
    [
        _file("../escape.txt"),
        _file("app/../../escape.txt"),
        _file("/etc/passwd"),
        _file("C:/Windows/evil.dll"),
        _file("app\\..\\evil.txt"),
        _file("a//b.txt"),
        _special("link", tarfile.SYMTYPE, "../../outside"),
        _special("inside-link", tarfile.SYMTYPE, "launcher.py"),
        _special("hard", tarfile.LNKTYPE, "/etc/passwd"),
        _special("tty", tarfile.CHRTYPE),
        _special("disk", tarfile.BLKTYPE),
        _special("pipe", tarfile.FIFOTYPE),
    ],
    ids=lambda member: member[0].name,
)
def test_unpack_refuses_unsafe_members_before_extracting_anything(tmp_path, member):
    archive = _tar(tmp_path / "part.tar.gz", [_file("launcher.py", b"ok"), member])
    target = tmp_path / "out"
    with pytest.raises(StageError) as refused:
        unpack(archive, target)
    assert refused.value.step == "unpack" and refused.value.retryable is False
    assert not (target / "launcher.py").exists()
    assert not (tmp_path / "escape.txt").exists()


def test_unpack_caps_entries_and_size_and_accepts_a_dot_root(tmp_path, monkeypatch):
    root = tarfile.TarInfo(".")
    root.type = tarfile.DIRTYPE
    good = _tar(tmp_path / "good.tar.gz", [
        (root, None),
        _file("./launcher.py", b"ok"),
        _file("src/row_bot/__init__.py", b""),
    ])
    assert unpack(good, tmp_path / "out") == 2
    assert (tmp_path / "out" / "launcher.py").read_bytes() == b"ok"

    monkeypatch.setattr(stager_module, "MAX_ARCHIVE_MEMBERS", 2)
    many = _tar(tmp_path / "many.tar.gz", [_file(f"f{i}") for i in range(3)])
    with pytest.raises(StageError, match="too many"):
        unpack(many, tmp_path / "many")
    monkeypatch.setattr(stager_module, "MAX_ARCHIVE_MEMBERS", 50_000)
    monkeypatch.setattr(stager_module, "MAX_ARCHIVE_BYTES", 10)
    big = _tar(tmp_path / "big.tar.gz", [_file("big.bin", b"x" * 11)])
    with pytest.raises(StageError, match="too large"):
        unpack(big, tmp_path / "big")


def _stage_with(
    tmp_path: Path,
    runner: FakeRunner,
    work_name: str = "work",
    *,
    release: Release | None = None,
    layout: InstallLayout | None = None,
) -> tuple[InstallLayout, UpdateEngine]:
    layout = layout or make_install(tmp_path)
    release = release or make_release()
    work_parent = tmp_path / work_name
    engine = UpdateEngine(
        layout,
        transport=FakeTransport(serve(release)),
        stager_factory=lambda layout_, **kwargs: Stager(layout_, runner=runner, clone_method="hardlink", **kwargs),
        activity=lambda: [],
        relaunch=lambda: True,
        environment=lambda: ({}, ["py3-none-any"]),
        python_version=lambda: "3.12.13",
        platform_id=lambda: "linux-x86_64",
        background=False,
        sleep=lambda _s: None,
    )
    engine.work_root = work_parent / ".updates"  # a work folder with an awkward name
    engine.start(release.files)
    return layout, engine


def test_pip_installs_only_hashed_binary_wheels_from_relative_paths(tmp_path):
    runner = FakeRunner()
    _layout, engine = _stage_with(tmp_path / "user #1", runner)
    assert engine.status().phase == "ready", engine.status()
    pip = next(command for command in runner.commands if "pip" in command)
    for flag in ("--no-index", "--no-deps", "--require-hashes", "--no-cache-dir"):
        assert flag in pip
    assert pip[pip.index("--only-binary") + 1] == ":all:"
    # Lines are relative to pip's working folder: a "#" in the user's path
    # (here "user #1") never reaches pip's requirement parser.
    assert runner.pip_lines and all(line.startswith("./") for line in runner.pip_lines)
    assert all("#" not in line.split(" --hash=")[0] for line in runner.pip_lines)
    assert all(" --hash=sha256:" in line for line in runner.pip_lines)
    assert "#" in str(runner.pip_cwd)


class _TrimmingRunner(FakeRunner):
    """pip brings back each wheel's tests and launcher; the staged trim runs in this process."""

    def __init__(self, **kwargs) -> None:  # noqa: ANN003
        super().__init__(**kwargs)
        self.trims: list[list[str]] = []

    def __call__(self, command, *, env, cwd, timeout, log):  # noqa: ANN001
        parts = [str(part) for part in command]
        code = super().__call__(command, env=env, cwd=cwd, timeout=timeout, log=log)
        if self.label(parts) == "pip":
            python = Path(parts[0])
            site = site_packages((python.parent if python.name == "python.exe" else python.parent.parent).parent)
            launchers = "../../Scripts" if os.name == "nt" else "../../../bin"
            for line in self.pip_lines:
                stem = Path(line.split(" --hash=")[0]).name.split("-")
                folder = stem[0].lower()
                add_dist(site, stem[0].replace("_", "-"), stem[1], {
                    f"{folder}/__init__.py": b"",
                    f"{folder}/tests/test_it.py": b"",
                    f"{folder}/only_in_the_new_rules/data.txt": b"",
                    f"{launchers}/{folder}{'.exe' if os.name == 'nt' else ''}": b"launcher",
                })
        if "trim" in parts and code == 0:
            self.trims.append(parts)
            # The staged interpreter is a stand-in: load the staged script itself.
            name = "staged_payload_trim"
            spec = importlib.util.spec_from_file_location(name, parts[parts.index("-S") + 1])
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module  # dataclasses look their module up while it loads
            try:
                spec.loader.exec_module(module)
                return module.main(parts[parts.index("trim"):])
            finally:
                sys.modules.pop(name, None)
        return code


def _release_with_trim_rules(lock: bytes = b"lock-2") -> Release:
    """A release whose code carries payload_trim, with one test-folder name the running rules lack."""
    rules = json.loads((ROW_BOT_SOURCE / "payload_trim.json").read_text(encoding="utf-8"))
    rules["site_packages"]["test_directories"]["names"].append("only_in_the_new_rules")
    return make_release(lock=lock, code={
        "launcher.py": b"# launcher 2\n",
        "src/row_bot/version.py": b'__version__ = "5.2.1"\n',
        "src/row_bot/payload_trim.py": (ROW_BOT_SOURCE / "payload_trim.py").read_bytes(),
        "src/row_bot/payload_trim.json": json.dumps(rules).encode(),
        "uv.lock": lock,
    })


def test_libraries_pip_reinstalls_are_trimmed_with_the_staged_versions_own_rules(tmp_path):
    runner = _TrimmingRunner()
    layout = make_install(tmp_path)
    running = tree_fingerprint(layout.version_root)
    _layout, engine = _stage_with(tmp_path, runner, release=_release_with_trim_rules(), layout=layout)
    assert engine.status().phase == "ready", engine.status()

    # The staged interpreter runs the staged version's script on the staged
    # runtime, without writing bytecode (-I alone ignores PYTHONDONTWRITEBYTECODE).
    [trim] = runner.trims
    staging = layout.versions_dir / "5.2.1.staging"
    assert Path(trim[0]).is_relative_to(staging / "python")
    assert trim[1:4] == ["-B", "-I", "-S"]
    assert Path(trim[4]) == staging / "app" / "src" / "row_bot" / "payload_trim.py"
    assert trim[5:] == ["trim", "--python-root", str(staging / "python")]
    labels = runner.labels()
    assert labels.index("pip") < labels.index("other") < labels.index("compile")

    site = site_packages(layout.versions_dir / "5.2.1")
    scripts = site.parents[1] / "Scripts" if os.name == "nt" else site.parents[2] / "bin"
    for library in ("alpha", "added"):
        assert (site / library / "__init__.py").is_file()
        assert not (site / library / "tests").exists()
        assert not (site / library / "only_in_the_new_rules").exists()  # the release's rules, not the running ones
        assert not (scripts / f"{library}{'.exe' if os.name == 'nt' else ''}").exists()
        assert any(site.glob(f"{library}-*.dist-info/RECORD"))
    assert tree_fingerprint(layout.version_root) == running


@pytest.mark.parametrize("case", ["no script in the release", "runtime reused", "trim fails"])
def test_the_trim_never_stops_an_update(tmp_path, case):
    if case == "no script in the release":
        runner, release = _TrimmingRunner(), make_release()
    elif case == "runtime reused":  # the running version's lock: nothing reinstalled, nothing to trim
        runner, release = _TrimmingRunner(), _release_with_trim_rules(lock=b"lock-1")
    else:
        runner, release = _TrimmingRunner(fail="other"), _release_with_trim_rules()
    _layout, engine = _stage_with(tmp_path, runner, release=release)
    assert engine.status().phase == "ready", engine.status()
    assert runner.trims == []
    assert ("other" in runner.labels()) is (case == "trim fails")


def test_quitting_stops_a_running_stage_command_and_the_engine(tmp_path):
    """Shutdown sets the engine's cancel; the stager's command is stopped, nothing is left."""
    started = threading.Event()

    class BlockingRunner(FakeRunner):
        def __call__(self, command, *, env, cwd, timeout, log):  # noqa: ANN001
            if self.label([str(part) for part in command]) == "dependencies":
                started.set()
                engine_ref[0]._cancel.wait(10)
                raise StageCancelled("stopped")
            return super().__call__(command, env=env, cwd=cwd, timeout=timeout, log=log)

    engine_ref: list[UpdateEngine] = []
    layout = make_install(tmp_path)
    release = make_release()
    engine = UpdateEngine(
        layout,
        transport=FakeTransport(serve(release)),
        stager_factory=lambda layout_, **kwargs: Stager(layout_, runner=BlockingRunner(), clone_method="hardlink",
                                                         **kwargs),
        activity=lambda: [], relaunch=lambda: True, environment=lambda: ({}, ["py3-none-any"]),
        python_version=lambda: "3.12.13", platform_id=lambda: "linux-x86_64", background=True,
        sleep=lambda _s: None,
    )
    engine_ref.append(engine)
    assert engine.start(release.files)
    assert started.wait(10)
    assert engine.shutdown(timeout=10) is True
    assert engine.status().phase == "idle"
    assert not (layout.versions_dir / "5.2.1.staging").exists()


@pytest.mark.slow
def test_a_cancelled_command_ends_with_its_whole_process_tree(tmp_path):
    """Real processes: a child that starts a grandchild; cancel stops both."""
    # The base interpreter, as a staged runtime is: a uv venv's python.exe is
    # a trampoline that runs the real one in a job of its own.
    python = getattr(sys, "_base_executable", None) or sys.executable
    marker = tmp_path / "grandchild.pid"
    script = (
        "import subprocess, sys, time\n"
        f"child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"open({str(marker)!r}, 'w').write(str(child.pid))\n"
        "time.sleep(60)\n"
    )
    cancel = threading.Event()
    result: list[BaseException] = []

    def run() -> None:
        try:
            run_command([python, "-c", script], env=dict(os.environ), cwd=tmp_path, timeout=120,
                        log=tmp_path / "log.txt", cancel=cancel)
        except BaseException as exc:  # noqa: BLE001
            result.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    deadline = time.monotonic() + 30
    written = ""
    while not written and time.monotonic() < deadline:
        written = marker.read_text().strip() if marker.exists() else ""
        if not written:
            time.sleep(0.1)
    grandchild = int(written)
    cancel.set()
    thread.join(30)
    assert isinstance(result[0], StageCancelled)
    import psutil

    deadline = time.monotonic() + 15
    while psutil.pid_exists(grandchild) and time.monotonic() < deadline:
        try:
            if psutil.Process(grandchild).status() == psutil.STATUS_ZOMBIE:
                break
        except psutil.NoSuchProcess:
            break
        time.sleep(0.1)
    alive = psutil.pid_exists(grandchild) and psutil.Process(grandchild).status() != psutil.STATUS_ZOMBIE
    assert not alive


def _wheel(folder: Path, name: str, version: str, body: str) -> Path:
    """A minimal pure-Python wheel."""
    dist = f"{name}-{version}"
    files = {
        f"{name}/__init__.py": body.encode(),
        f"{dist}.dist-info/METADATA": f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n".encode(),
        f"{dist}.dist-info/WHEEL": b"Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    }
    record = "".join(f"{path},sha256={_record_hash(data)},{len(data)}\n" for path, data in files.items())
    record += f"{dist}.dist-info/RECORD,,\n"
    path = folder / f"{dist}-py3-none-any.whl"
    with zipfile.ZipFile(path, "w") as archive:
        for name_in_zip, data in files.items():
            archive.writestr(name_in_zip, data)
        archive.writestr(f"{dist}.dist-info/RECORD", record)
    return path


def _record_hash(data: bytes) -> str:
    import base64
    import hashlib

    return base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()


@pytest.mark.slow
def test_a_real_pip_install_into_a_hard_linked_clone_leaves_the_running_runtime_alone(tmp_path):
    """Real pip in a real (small) runtime, from a path with "#" in it."""
    import subprocess

    from row_bot.updates.planner import InstalledDist, RuntimePlan, WheelPick, installed_distributions, prune_paths

    import shutil

    base = tmp_path / "user #1"
    runtime = base / "versions" / "1.0" / "python"
    if os.name == "nt":
        # The launchers start python/python.exe at the runtime's root, as in a
        # standalone Python home (a venv keeps it in Scripts): copy the base one.
        shutil.copytree(
            sys.base_prefix, runtime,
            ignore=shutil.ignore_patterns("__pycache__", "tcl", "include", "libs", "Scripts", "test", "idlelib",
                                          "tkinter", "turtledemo"),
        )
        python = runtime / "python.exe"
        site_rel = "Lib/site-packages"
        if not (runtime / site_rel / "pip").is_dir():
            pytest.skip("the base Python has no pip")
    else:
        subprocess.run([sys.executable, "-m", "venv", str(runtime)], check=True, capture_output=True)
        python = runtime / "bin" / "python3"
        site_rel = next(runtime.glob("lib/python3.*/site-packages")).relative_to(runtime).as_posix()
    quiet = {key: value for key, value in os.environ.items()
             if not key.upper().startswith(("PYTHON", "VIRTUAL_ENV", "PIP_", "__PYVENV"))}
    quiet.update({"PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1"})
    wheels = base / "wheels"
    wheels.mkdir(parents=True)
    old = _wheel(wheels, "tinylib", "1.0", "VERSION = '1.0'\n")
    seeded = subprocess.run([str(python), "-m", "pip", "install", "--no-index", "--no-deps", "--no-cache-dir",
                             "--disable-pip-version-check", "--break-system-packages", str(old)],
                            capture_output=True, text=True, env=quiet)
    assert seeded.returncode == 0, seeded.stdout + seeded.stderr
    site = runtime / site_rel
    installed = installed_distributions(site)
    new = _wheel(wheels, "tinylib", "1.1", "VERSION = '1.1'\n")
    before = {path.relative_to(runtime).as_posix(): (sha256(path.read_bytes()), path.stat().st_ino)
              for path in runtime.rglob("*") if path.is_file()}

    pick = WheelPick("tinylib", "1.0", "1.1", "https://files.pythonhosted.org/tinylib.whl", new.name,
                     sha256(new.read_bytes()), new.stat().st_size)
    plan = RuntimePlan("sync", "test", change=[pick])
    staging = base / "versions" / "1.1.staging"
    skip = prune_paths(plan, installed, site_packages=site_rel)
    clone_tree(runtime, staging / "python", method="hardlink", skip=skip)
    layout = InstallLayout(make_install(tmp_path / "unused").kind, base, "1.0", runtime.parent)
    (base / "work").mkdir()
    stager = Stager(layout, work_dir=base / "work")
    stager._install_wheels(
        stager_module.StageInputs(manifest=None, runtime=plan, installed=installed,  # type: ignore[arg-type]
                                  code_archive=Path("unused"), assets_archive=None, wheels={"tinylib": new}),
        staging,
    )

    after = {path.relative_to(runtime).as_posix(): (sha256(path.read_bytes()), path.stat().st_ino)
             for path in runtime.rglob("*") if path.is_file()}
    assert after == before
    staged_site = staging / "python" / site_rel
    assert (staged_site / "tinylib" / "__init__.py").read_text() == "VERSION = '1.1'\n"
    assert (site / "tinylib" / "__init__.py").read_text() == "VERSION = '1.0'\n"
    assert isinstance(installed["tinylib"], InstalledDist)
