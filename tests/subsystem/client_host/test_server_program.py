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
