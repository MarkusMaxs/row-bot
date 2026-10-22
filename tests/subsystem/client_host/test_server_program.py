"""On Windows the server runs as Row-Bot Server.exe, so Windows Firewall names Row-Bot, not Python (B355).

What the server starts keeps running as the python.exe beside it.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from row_bot import runtime_paths

pytestmark = [pytest.mark.subsystem, pytest.mark.platform]


def _python(tmp_path: Path, *, branded: bool) -> Path:
    python = tmp_path / "versions" / "5.2.0" / "python" / "python.exe"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"MZ")
    if branded:
        (python.parent / runtime_paths.WINDOWS_SERVER_EXE).write_bytes(b"MZ")
    return python


@pytest.mark.parametrize(("platform", "branded", "expected"), [
    ("win32", True, runtime_paths.WINDOWS_SERVER_EXE),
    ("win32", False, "python.exe"),  # a source checkout or an install from before it
    ("darwin", True, "python.exe"),
])
def test_the_server_runs_as_row_bot_where_a_windows_install_has_it(tmp_path, monkeypatch, platform, branded,
                                                                   expected) -> None:
    python = _python(tmp_path, branded=branded)
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(sys, "executable", str(python))

    assert Path(runtime_paths.server_python()) == python.with_name(expected)


def test_what_the_server_starts_runs_as_python_beside_it(tmp_path, monkeypatch) -> None:
    python = _python(tmp_path, branded=True)
    monkeypatch.setattr(sys, "executable", str(python.with_name(runtime_paths.WINDOWS_SERVER_EXE)))
    monkeypatch.setattr(sys, "_base_executable", sys.executable, raising=False)

    runtime_paths.python_for_children()

    assert (sys.executable, sys._base_executable) == (str(python), str(python))


def test_an_ordinary_python_is_left_alone(tmp_path, monkeypatch) -> None:
    python = _python(tmp_path, branded=True)
    monkeypatch.setattr(sys, "executable", str(python))
    monkeypatch.setattr(sys, "_base_executable", "base-python", raising=False)

    runtime_paths.python_for_children()

    assert (sys.executable, sys._base_executable) == (str(python), "base-python")


# ── B365: one path for every version, so Windows Firewall asks once ─────────

_PTH = "\n".join(["python313.zip", ".", r"Lib\site-packages", r"..\app", r"..\app\src", "Lib", "import site"]) + "\n"


def _install_version(root: Path, version: str, *, server: bytes = b"MZ signed") -> Path:
    """A per-user install's version folder with the files the server copy needs."""
    python = root / "versions" / version / "python"
    (python / "Lib" / "site-packages").mkdir(parents=True)
    (root / "versions" / version / "app").mkdir()
    for name in ("python.exe", "python3.dll", "python313.dll", "vcruntime140.dll"):
        (python / name).write_bytes(f"{name} {version}".encode())
    (python / runtime_paths.WINDOWS_SERVER_EXE).write_bytes(server)
    (python / "python313._pth").write_text(_PTH, encoding="utf-8", newline="\n")
    return python


windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows paths in the ._pth")


def _run_version(monkeypatch, python: Path) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "executable", str(python / "python.exe"))
    monkeypatch.setattr(sys, "version_info", (3, 13, 2, "final", 0))
    monkeypatch.setenv("ROW_BOT_APP_ROOT", str(python.parent / "app"))


@windows_only
def test_the_server_runs_from_the_same_path_before_and_after_an_update(tmp_path, monkeypatch) -> None:
    root = tmp_path / "Row-Bot"
    _run_version(monkeypatch, _install_version(root, "5.3.0"))
    first = Path(runtime_paths.server_python())

    python = _install_version(root, "5.3.1")
    _run_version(monkeypatch, python)
    second = Path(runtime_paths.server_python())

    assert first == second == root / "versions" / runtime_paths.WINDOWS_SERVER_EXE
    assert second.read_bytes() == b"MZ signed"  # the version's own signed program, copied as it is
    assert (root / "versions" / "python313.dll").read_bytes() == b"python313.dll 5.3.1"
    lines = (root / "versions" / "python313._pth").read_text(encoding="utf-8").splitlines()
    assert lines[:3] == [str(python / "python313.zip"), str(python), str(python / "Lib" / "site-packages")]
    assert lines[3:5] == [str(python.parent / "app"), str(python.parent / "app" / "src")]
    assert lines[-1] == "import site"


@windows_only
def test_the_server_runs_beside_python_when_the_copy_cannot_be_replaced(tmp_path, monkeypatch) -> None:
    root = tmp_path / "Row-Bot"
    _run_version(monkeypatch, _install_version(root, "5.3.0"))
    runtime_paths.server_python()
    python = _install_version(root, "5.3.1", server=b"MZ newer")
    _run_version(monkeypatch, python)

    def in_use(source, target):
        raise PermissionError(32, "in use by another Row-Bot", str(target))

    monkeypatch.setattr(runtime_paths.os, "replace", in_use)

    assert Path(runtime_paths.server_python()) == python / runtime_paths.WINDOWS_SERVER_EXE
    assert not list((root / "versions").glob("*.tmp"))


@windows_only
def test_an_update_to_a_newer_python_leaves_no_older_python_beside_the_server(tmp_path, monkeypatch) -> None:
    root = tmp_path / "Row-Bot"
    _run_version(monkeypatch, _install_version(root, "5.3.0"))
    runtime_paths.server_python()
    python = _install_version(root, "5.4.0")
    for name in ("python313.dll", "python313._pth"):
        (python / name).rename(python / name.replace("313", "314"))
    _run_version(monkeypatch, python)
    monkeypatch.setattr(sys, "version_info", (3, 14, 0, "final", 0))

    runtime_paths.server_python()

    assert sorted(path.name for path in (root / "versions").glob("python3*")) == [
        "python3.dll", "python314._pth", "python314.dll"]


def test_the_copy_takes_the_version_as_its_python(tmp_path, monkeypatch) -> None:
    root = tmp_path / "Row-Bot"
    python = _install_version(root, "5.3.0")
    site_packages = python / "Lib" / "site-packages"
    (site_packages / "extra").mkdir()
    (site_packages / "probe.pth").write_text("extra\n", encoding="utf-8")
    host = root / "versions" / runtime_paths.WINDOWS_SERVER_EXE
    for name in ("executable", "_base_executable", "prefix", "base_prefix", "exec_prefix", "base_exec_prefix"):
        monkeypatch.setattr(sys, name, str(host), raising=False)
    monkeypatch.setattr(sys, "path", [str(site_packages), str(host.parent)])
    monkeypatch.setattr(runtime_paths, "APP_ROOT", python.parent / "app")

    runtime_paths.python_for_children()

    assert (sys.executable, sys._base_executable) == (str(python / "python.exe"),) * 2
    assert {sys.prefix, sys.base_prefix, sys.exec_prefix, sys.base_exec_prefix} == {str(python)}
    assert sys.path == [str(site_packages), str(site_packages / "extra")]  # .pth read, the host's folder gone
