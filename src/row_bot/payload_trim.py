"""What a packaged Row-Bot runtime leaves out, and which bytecode it ships.

The rules live in ``payload_trim.json`` next to this file, so the three
installer builds (``installer/build_installer.ps1``, ``build_mac_app.sh`` and
``build_linux_app.sh``) apply the same ones, and so can the update stager on a
staged version:

- ``trim``: removes libraries' test suites, C++ headers, link-time libraries,
  debug symbols, unused Google API discovery documents, pip's console-script
  launchers and the standard library's test suite. ``.dist-info`` folders are
  never touched.
- ``bytecode``: removes any ``__pycache__`` and compiles deterministic,
  path-independent bytecode (``unchecked-hash``) for the app and for the
  runtime's modules that the runtime check imports. Version folders are never
  written after install, so nothing else ever gets bytecode.

The module uses the standard library only, so it runs as a script
(``python -I -S payload_trim.py ...``) with the bundled interpreter.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

logger = logging.getLogger(__name__)

RULES_FILE = Path(__file__).with_name("payload_trim.json")
BYTECODE_FLAGS = ("--invalidation-mode", "unchecked-hash")

# Run by the bundled interpreter: the runtime check, then the source files of
# every module it imported, one per line.
_TRACE = """\
import atexit, os, runpy, sys
out, script = sys.argv[1], sys.argv[2]
def _dump():
    files = set()
    for module in list(sys.modules.values()):
        path = getattr(module, "__file__", None)
        if isinstance(path, str) and path.endswith(".py"):
            files.add(os.path.realpath(path))
    with open(out, "w", encoding="utf-8") as stream:
        stream.write("\\n".join(sorted(files)) + "\\n")
atexit.register(_dump)
sys.argv = [script, *sys.argv[3:]]
runpy.run_path(script, run_name="__main__")
"""


def load_rules(path: Path | None = None) -> dict[str, Any]:
    rules = json.loads((path or RULES_FILE).read_text(encoding="utf-8"))
    if rules.get("schema") != 1:
        raise ValueError(f"unsupported payload rules schema: {rules.get('schema')!r}")
    return rules


# ── Runtime layout ──────────────────────────────────────────────────────────


def is_windows_runtime(python_root: Path) -> bool:
    """Windows' embedded Python keeps ``python.exe`` at its root (macOS and Linux: ``bin/python3``).

    Not ``Lib``: on a case-insensitive macOS volume ``Lib`` names ``lib``.
    """
    return (Path(python_root) / "python.exe").is_file()


def stdlib_dir(python_root: Path) -> Path:
    """``python/Lib`` (Windows embedded Python) or ``python/lib/python3.X``."""
    if is_windows_runtime(python_root):
        return Path(python_root) / "Lib"
    found = sorted(Path(python_root).glob("lib/python3.*"))
    found = [path for path in found if (path / "site-packages").is_dir()] or found
    if not found:
        raise FileNotFoundError(f"no standard library folder under {python_root}")
    return found[0]


def site_packages_dir(python_root: Path) -> Path:
    return stdlib_dir(python_root) / "site-packages"


def scripts_dir(python_root: Path) -> Path:
    return Path(python_root) / ("Scripts" if is_windows_runtime(python_root) else "bin")


def python_executable(python_root: Path) -> Path:
    for candidate in (Path(python_root) / "python.exe", Path(python_root) / "bin" / "python3"):
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(f"no Python interpreter under {python_root}")


# ── Trim ────────────────────────────────────────────────────────────────────


@dataclass
class TrimReport:
    """Files and bytes removed per rule."""

    removed: dict[str, list[int]] = field(default_factory=dict)

    def add(self, rule: str, files: int, size: int) -> None:
        entry = self.removed.setdefault(rule, [0, 0])
        entry[0] += files
        entry[1] += size

    @property
    def files(self) -> int:
        return sum(files for files, _size in self.removed.values())

    @property
    def bytes(self) -> int:
        return sum(size for _files, size in self.removed.values())

    def lines(self) -> list[str]:
        rows = [f"{rule}: {files} files, {size / 1e6:.1f} MB" for rule, (files, size) in sorted(self.removed.items())]
        rows.append(f"total: {self.files} files, {self.bytes / 1e6:.1f} MB")
        return rows


def _tree_size(path: Path) -> tuple[int, int]:
    if path.is_symlink() or path.is_file():
        return 1, path.lstat().st_size
    files = size = 0
    for dirpath, _dirnames, filenames in os.walk(path):
        for name in filenames:
            try:
                size += (Path(dirpath) / name).lstat().st_size
            except OSError:
                continue
            files += 1
    return files, size


def _remove(path: Path, rule: str, report: TrimReport) -> None:
    files, size = _tree_size(path)
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()
    report.add(rule, files, size)


def _remove_tree_except(path: Path, keep: set[Path], rule: str, report: TrimReport) -> None:
    """Remove *path* but the files in *keep* (and the folders leading to them)."""
    if not any(_is_within(kept, path) for kept in keep):
        _remove(path, rule, report)
        return
    for child in sorted(path.iterdir()):
        if child in keep:
            continue
        if child.is_dir() and not child.is_symlink():
            _remove_tree_except(child, keep, rule, report)
        else:
            _remove(child, rule, report)


def _is_within(path: Path, folder: Path) -> bool:
    try:
        path.relative_to(folder)
    except ValueError:
        return False
    return True


def _record_scripts(site: Path, scripts: Path) -> set[Path]:
    """The launchers and scripts pip installed into *scripts*, as each RECORD lists them."""
    found: set[Path] = set()
    scripts_resolved = Path(os.path.normcase(os.path.abspath(scripts)))
    for record in sorted(site.glob("*.dist-info/RECORD")):
        try:
            text = record.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            entry = line.rsplit(",", 2)[0].strip().strip('"')
            if not entry:
                continue
            target = Path(os.path.normcase(os.path.abspath(site / entry)))
            if target.parent == scripts_resolved:
                found.add(scripts / Path(os.path.abspath(site / entry)).name)
    return found


def trim(python_root: Path, rules: Mapping[str, Any] | None = None) -> TrimReport:
    """Remove what the rules leave out of the runtime at *python_root*. Idempotent."""
    rules = rules or load_rules()
    python_root = Path(python_root)
    site = site_packages_dir(python_root)
    report = TrimReport()
    site_rules = rules["site_packages"]

    for item in site_rules.get("remove", []):
        target = site / Path(item["path"])
        if target.exists() or target.is_symlink():
            _remove(target, "headers and build files", report)

    discovery = site_rules["discovery_documents"]
    documents = site / Path(discovery["path"])
    if documents.is_dir():
        kept = set(discovery["keep"])
        for document in sorted(documents.glob("*.json")):
            if document.name not in kept:
                _remove(document, "Google API discovery documents", report)

    keep = {site / Path(item["path"]) for item in site_rules.get("keep", [])}
    test_names = set(site_rules["test_directories"]["names"])
    dsym_suffixes = tuple(site_rules["remove_directory_suffixes"]["suffixes"])
    file_suffixes = tuple(suffix.lower() for suffix in site_rules["remove_suffixes"]["suffixes"])
    for dirpath, dirnames, filenames in os.walk(site, topdown=True):
        folder = Path(dirpath)
        for name in sorted(dirnames):
            child = folder / name
            if name in test_names:
                _remove_tree_except(child, keep, "test suites", report)
            elif name.endswith(dsym_suffixes):
                _remove(child, "debug symbols", report)
            else:
                continue
            if not child.exists():
                dirnames.remove(name)
        for name in filenames:
            if name.lower().endswith(file_suffixes):
                _remove(folder / name, "link libraries and debug symbols", report)

    scripts = scripts_dir(python_root)
    if scripts.is_dir():
        for script in sorted(_record_scripts(site, scripts)):
            if script.is_file() or script.is_symlink():
                _remove(script, "console-script launchers", report)

    stdlib = stdlib_dir(python_root)
    for relative in rules["stdlib"]["remove"]:
        target = stdlib / Path(relative)
        if target.is_dir() and target != site:
            _remove(target, "standard library tests", report)
    return report


# ── Bytecode ────────────────────────────────────────────────────────────────


def runtime_environment(python_root: Path, data_dir: Path, base: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment the native launchers give the bundled Python, with a throwaway data folder."""
    env = {key: value for key, value in (os.environ if base is None else base).items()
           if not key.upper().startswith(("PYTHON", "VIRTUAL_ENV"))}
    env.update({
        "PYTHONNOUSERSITE": "1",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "ROW_BOT_DATA_DIR": str(data_dir),
    })
    python_root = Path(python_root)
    if is_windows_runtime(python_root):
        env["TCL_LIBRARY"] = str(python_root / "tcl" / "tcl8.6")
        env["TK_LIBRARY"] = str(python_root / "tcl" / "tk8.6")
        env["PATH"] = os.pathsep.join([str(python_root / "Scripts"), str(python_root), env.get("PATH", "")])
    if (python_root / "playwright-browsers").is_dir():
        env["PLAYWRIGHT_BROWSERS_PATH"] = str(python_root / "playwright-browsers")
    return env


def remove_bytecode(roots: Iterable[Path]) -> int:
    """Remove every ``__pycache__`` folder under *roots*; returns the files removed."""
    removed = 0
    for root in roots:
        for dirpath, dirnames, _filenames in os.walk(root):
            if "__pycache__" in dirnames:
                cache = Path(dirpath) / "__pycache__"
                removed += _tree_size(cache)[0]
                shutil.rmtree(cache)
                dirnames.remove("__pycache__")
    return removed


def traced_sources(listing: str, python_root: Path) -> list[Path]:
    """The ``.py`` files under *python_root* named in a trace listing, sorted."""
    root = Path(os.path.realpath(python_root))
    selected = set()
    for line in listing.splitlines():
        line = line.strip()
        if not line.endswith(".py"):
            continue
        path = Path(line)
        if _is_within(path, root) and path.is_file():
            selected.add(path)
    return sorted(selected)


Runner = Callable[..., "subprocess.CompletedProcess[Any]"]


def compile_payload(
    python_root: Path,
    app_root: Path,
    version_root: Path,
    *,
    rules: Mapping[str, Any] | None = None,
    python: Path | None = None,
    runner: Runner = subprocess.run,
    environ: Mapping[str, str] | None = None,
) -> dict[str, int]:
    """Ship the bytecode the rules ask for, and nothing else.

    Removes every ``__pycache__`` under *python_root* and *app_root*, runs the
    runtime check with the bundled Python to see which runtime modules it
    imports, then compiles the app and those modules. The app must compile;
    runtime modules compile leniently, as they always have.
    """
    rules = rules or load_rules()
    # Real paths throughout: compileall strips --version-root from the paths it
    # records only where they match part for part.
    python_root, app_root, version_root = (Path(os.path.realpath(path)) for path in (python_root, app_root, version_root))
    interpreter = Path(python) if python else python_executable(python_root)
    removed = remove_bytecode([python_root, app_root])
    trace = [str(version_root / item) if item.startswith("app/") else item for item in rules["bytecode"]["trace"]]
    with tempfile.TemporaryDirectory(prefix="row-bot-bytecode-") as work:
        work_dir = Path(work)
        listing = work_dir / "imported.txt"
        env = runtime_environment(python_root, work_dir / "data", environ)
        (work_dir / "data").mkdir()
        script = work_dir / "trace.py"
        script.write_text(_TRACE, encoding="utf-8")
        traced = runner([str(interpreter), str(script), str(listing), *trace], env=env, cwd=str(app_root),
                        check=False)
        if traced.returncode != 0 or not listing.is_file():
            raise RuntimeError(f"the runtime check failed while tracing imports (exit {traced.returncode})")
        sources = traced_sources(listing.read_text(encoding="utf-8"), python_root)
        compile_env = runtime_environment(python_root, work_dir / "data", environ)
        compile_env.pop("PYTHONDONTWRITEBYTECODE")
        base = [str(interpreter), "-m", "compileall", "-q", "-f", "-s", str(version_root), *BYTECODE_FLAGS]
        app = runner([*base, "-j", "0", str(app_root)], env=compile_env, cwd=str(version_root), check=False)
        if app.returncode != 0:
            raise RuntimeError(f"compiling the app's bytecode failed (exit {app.returncode})")
        files = work_dir / "compile.txt"
        files.write_text("".join(f"{path}\n" for path in sources), encoding="utf-8")
        libraries = runner([*base, "-i", str(files)], env=compile_env, cwd=str(version_root), check=False)
        if libraries.returncode != 0:
            logger.warning("Some runtime modules did not compile; they run from source.")
    return {"removed": removed, "runtime_modules": len(sources)}


# ── Command line ────────────────────────────────────────────────────────────


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Row-Bot payload rules: trim a runtime, compile its bytecode")
    sub = parser.add_subparsers(dest="command", required=True)
    trim_parser = sub.add_parser("trim", help="remove what a packaged runtime leaves out")
    trim_parser.add_argument("--python-root", required=True, type=Path)
    code_parser = sub.add_parser("bytecode", help="compile the bytecode a packaged version ships")
    code_parser.add_argument("--python-root", required=True, type=Path)
    code_parser.add_argument("--app-root", required=True, type=Path)
    code_parser.add_argument("--version-root", required=True, type=Path,
                             help="the folder holding app/ and python/; recorded paths are relative to it")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if args.command == "trim":
        report = trim(args.python_root)
        for line in report.lines():
            print(f"      trimmed {line}")
        return 0
    try:
        result = compile_payload(args.python_root, args.app_root, args.version_root)
    except (OSError, RuntimeError) as exc:
        print(f"payload_trim: {exc}", file=sys.stderr)
        return 1
    print(f"      removed {result['removed']} bytecode files; compiled the app and "
          f"{result['runtime_modules']} runtime modules")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
