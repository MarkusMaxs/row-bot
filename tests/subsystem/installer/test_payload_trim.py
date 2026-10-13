"""What a packaged runtime leaves out, and which bytecode it ships (src/row_bot/payload_trim.py).

The three installer builds run ``payload_trim.py trim`` and ``payload_trim.py
bytecode`` on the runtime they bundle; installer-verify then checks the result
with the runtime check, the client asset check and the app smokes.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from row_bot import payload_trim


pytestmark = [pytest.mark.subsystem, pytest.mark.installer, pytest.mark.platform]


def _write(path: Path, text: str = "x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _runtime(root: Path, kind: str) -> tuple[Path, Path, Path, str, Path]:
    """A runtime laid out as Windows' embedded Python or as python-build-standalone.

    Returns the python root, site-packages, the launcher folder, how RECORD
    files reach that folder, and the standard library folder.
    """
    python = root / "python"
    if kind == "windows":
        _write(python / "python.exe")
        stdlib = python / "Lib"
        scripts, record_prefix = python / "Scripts", "../../Scripts"
    else:
        _write(python / "bin" / "python3")
        stdlib = python / "lib" / "python3.12"
        scripts, record_prefix = python / "bin", "../../../bin"
    site = stdlib / "site-packages"
    site.mkdir(parents=True)
    return python, site, scripts, record_prefix, stdlib


def _files(root: Path) -> set[str]:
    return {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}


@pytest.mark.parametrize("kind", ["windows", "posix"])
def test_trim_leaves_out_tests_headers_link_libraries_discovery_documents_and_launchers(
    tmp_path: Path, kind: str
) -> None:
    python, site, scripts, prefix, stdlib = _runtime(tmp_path, kind)
    keep = {
        "pkg/__init__.py", "pkg/core.py", "pkg/testing/__init__.py", "pkg/data/table.csv",
        "numpy/_core/tests/_natype.py",
        "torch/__init__.py", "torch/lib/torch_cpu.dll", "torch/lib/libtorch_cpu.so",
        "googleapiclient/discovery_cache/documents/gmail.v1.json",
        "googleapiclient/discovery_cache/documents/calendar.v3.json",
        "googleapiclient/discovery_cache/__init__.py",
        "pkg-1.0.dist-info/METADATA", "pkg-1.0.dist-info/RECORD", "pkg-1.0.dist-info/LICENSE",
        "pkg-1.0.dist-info/entry_points.txt", "pkg-1.0.dist-info/licenses/NOTICE",
    }
    gone = {
        "pkg/tests/__init__.py", "pkg/tests/test_core.py", "pkg/tests/data/blob.npz",
        "pkg/sub/test/test_sub.py",
        "numpy/_core/tests/test_multiarray.py", "numpy/_core/tests/data/umath.csv",
        "torch/include/c10/core/Device.h", "torch/share/cmake/Torch/TorchConfig.cmake",
        "torch/lib/torch_cpu.lib", "torch/lib/torch_cpu.pdb", "torch/lib/c10.exp", "numpy/_core/lib/libnpymath.a",
        "PyObjCTest/helper.so.dSYM/Contents/Resources/DWARF/helper.so",
        "googleapiclient/discovery_cache/documents/drive.v3.json",
        "googleapiclient/discovery_cache/documents/youtube.v3.json",
    }
    for relative in keep | gone:
        _write(site / relative)
    record = [f"{prefix}/pkg{'.exe' if kind == 'windows' else ''},sha256=abc,10",
              f"{prefix}/pkg-gui{'.exe' if kind == 'windows' else ''},,",
              f'"{prefix}/odd, name.py",,', "pkg/__init__.py,sha256=def,1", "pkg-1.0.dist-info/RECORD,,"]
    _write(site / "pkg-1.0.dist-info" / "RECORD", "\n".join(record) + "\n")
    launchers = {f"pkg{'.exe' if kind == 'windows' else ''}", f"pkg-gui{'.exe' if kind == 'windows' else ''}",
                 "odd, name.py"}
    for name in launchers:
        _write(scripts / name)
    _write(scripts / "not-from-a-library.cmd")
    stdlib_keep = {"json/__init__.py"}
    stdlib_gone = {"test/test_os.py", "ensurepip/__init__.py", "idlelib/idle_test/test_x.py"}
    for relative in stdlib_keep | stdlib_gone:
        _write(stdlib / relative)
    before_scripts = _files(scripts)

    report = payload_trim.trim(python)

    assert _files(site) == keep
    assert _files(scripts) == before_scripts - launchers
    remaining_stdlib = {path for path in _files(stdlib) if not path.startswith("site-packages/")}
    assert remaining_stdlib == stdlib_keep
    assert report.files == len(gone) + len(launchers) + len(stdlib_gone)
    assert set(report.removed) == {
        "test suites", "link libraries and debug symbols", "debug symbols", "headers and build files",
        "Google API discovery documents", "console-script launchers", "standard library tests",
    }

    # A second run (an update staging an already trimmed runtime) changes nothing.
    snapshot = _files(python)
    again = payload_trim.trim(python)
    assert again.files == 0
    assert _files(python) == snapshot


def test_trim_keeps_test_folders_runtime_code_imports_at_any_depth(tmp_path: Path) -> None:
    python, site, *_ = _runtime(tmp_path, "posix")
    rules = payload_trim.load_rules()
    rules["site_packages"]["keep"] = [{"path": "lib/a/tests/deep/helper.py", "why": "imported"}]
    _write(site / "lib" / "a" / "tests" / "deep" / "helper.py")
    _write(site / "lib" / "a" / "tests" / "deep" / "test_helper.py")
    _write(site / "lib" / "a" / "tests" / "test_a.py")

    payload_trim.trim(python, rules)

    assert _files(site) == {"lib/a/tests/deep/helper.py"}


def _fake_runner(listing: str, calls: list[SimpleNamespace], *, trace_exit: int = 0):
    def run(command, *, env, cwd, check):  # noqa: ANN001
        calls.append(SimpleNamespace(command=list(command), env=dict(env), cwd=cwd))
        if "compileall" not in command:
            Path(command[2]).write_text(listing, encoding="utf-8")
            return subprocess.CompletedProcess(command, trace_exit)
        if "-i" in command:
            calls[-1].files = Path(command[command.index("-i") + 1]).read_text(encoding="utf-8").split()
        return subprocess.CompletedProcess(command, 0)

    return run


def test_bytecode_compiles_the_app_and_only_the_runtime_modules_the_runtime_check_imports(tmp_path: Path) -> None:
    root = tmp_path / "Row-Bot"
    python, site, *_ = _runtime(root, "posix")
    app = root / "app"
    _write(app / "scripts" / "verify_runtime_dependencies.py")
    stale = [_write(site / "unused" / "__pycache__" / "mod.cpython-312.pyc"),
             _write(app / "src" / "row_bot" / "__pycache__" / "old.cpython-312.pyc")]
    used = [_write(site / "used" / "__init__.py"), _write(site / "used" / "core.py")]
    _write(site / "unused" / "mod.py")
    elsewhere = _write(tmp_path / "outside" / "mod.py")
    listing = "\n".join([str(used[1]), str(used[0]), str(elsewhere), str(site / "native.so"), str(site / "gone.py")])
    calls: list[SimpleNamespace] = []
    real = Path(os.path.realpath(root))

    result = payload_trim.compile_payload(python, app, root, runner=_fake_runner(listing, calls),
                                          environ={"PATH": "x", "PYTHONPATH": "/elsewhere"})

    assert not any(path.exists() for path in stale)
    trace, app_compile, libraries = calls
    assert trace.command[3:] == [str(real / "app" / "scripts" / "verify_runtime_dependencies.py"), "all"]
    assert trace.env["PYTHONDONTWRITEBYTECODE"] == "1" and "PYTHONPATH" not in trace.env
    assert Path(trace.env["ROW_BOT_DATA_DIR"]).name == "data"
    for call in (app_compile, libraries):
        assert call.command[1:3] == ["-m", "compileall"]
        assert call.command[call.command.index("-s") + 1] == str(real)
        assert call.command[call.command.index("--invalidation-mode") + 1] == "unchecked-hash"
        assert "PYTHONDONTWRITEBYTECODE" not in call.env
    assert app_compile.command[-1] == str(real / "app")
    assert libraries.files == sorted(str(Path(os.path.realpath(path))) for path in used)
    assert result == {"removed": 2, "runtime_modules": 2}


def test_bytecode_stops_when_the_runtime_check_fails(tmp_path: Path) -> None:
    python, site, *_ = _runtime(tmp_path, "windows")
    app = tmp_path / "app"
    app.mkdir()
    calls: list[SimpleNamespace] = []

    with pytest.raises(RuntimeError, match="runtime check failed"):
        payload_trim.compile_payload(python, app, tmp_path, runner=_fake_runner("", calls, trace_exit=1))

    assert len(calls) == 1


@pytest.mark.slow
def test_shipped_bytecode_is_hash_based_path_independent_and_only_for_imported_modules(tmp_path: Path) -> None:
    root = tmp_path / "Row-Bot"
    python, site, *_ = _runtime(root, "posix")
    app = root / "app"
    _write(app / "src" / "row_bot" / "thing.py", "VALUE = 1\n")
    # The stand-in runtime check imports one library the way the real one does.
    _write(app / "scripts" / "verify_runtime_dependencies.py",
           f"import sys\nsys.path.insert(0, {str(site)!r})\nimport used\n")
    _write(site / "used" / "__init__.py", "from . import core\n")
    _write(site / "used" / "core.py", "X = 1\n")
    _write(site / "unused" / "__init__.py", "Y = 1\n")

    payload_trim.compile_payload(python, app, root, python=Path(sys.executable))

    tag = sys.implementation.cache_tag
    compiled = sorted(path.relative_to(root).as_posix() for path in root.rglob("*.pyc"))
    assert compiled == sorted([
        f"app/scripts/__pycache__/verify_runtime_dependencies.{tag}.pyc",
        f"app/src/row_bot/__pycache__/thing.{tag}.pyc",
        f"python/lib/python3.12/site-packages/used/__pycache__/__init__.{tag}.pyc",
        f"python/lib/python3.12/site-packages/used/__pycache__/core.{tag}.pyc",
    ])
    for path in root.rglob("*.pyc"):
        data = path.read_bytes()
        assert int.from_bytes(data[4:8], "little") == 1  # hash-based, never checked against the source
    import marshal

    code = marshal.loads((site / "used" / "__pycache__" / f"core.{tag}.pyc").read_bytes()[16:])
    assert Path(code.co_filename).as_posix() == "python/lib/python3.12/site-packages/used/core.py"
