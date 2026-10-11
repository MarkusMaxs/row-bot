"""Build one installed-app payload from a checked-out Row-Bot tag (Phase 1 spike).

Runs the tag's own installer build script, patched in memory only to:
  * install with pip --no-compile and then compile deterministically with
    `compileall -s <stage root> --invalidation-mode unchecked-hash`
    (path-independent .pyc, Phase 0 lesson),
  * stop before Inno Setup (Windows), pkgbuild (macOS) or the tarball (Linux).

Output (--out):
  Windows: python/ and app/            (what versions/<v>/ holds)
  macOS:   Row-Bot.app                 (unsigned; the workflow signs it)
  Linux:   the package root            (what releases/<v>/ holds)

  python build_payload.py --os windows|macos|linux --repo rb --version 5.0.0 --out <dir> --report <json>
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

TIMINGS: dict[str, float] = {}


def run(cmd: list, cwd: Path | None = None, env: dict | None = None, check: bool = True) -> int:
    print("+", " ".join(str(c) for c in cmd), flush=True)
    proc = subprocess.run([str(c) for c in cmd], cwd=cwd, env=env)  # noqa: S603 - CI build tool
    if check and proc.returncode != 0:
        raise SystemExit(f"command failed ({proc.returncode}): {cmd}")
    return proc.returncode


class timed:  # noqa: N801
    def __init__(self, name: str) -> None:
        self.name = name

    def __enter__(self) -> None:
        self.t = time.time()

    def __exit__(self, *exc: object) -> None:
        TIMINGS[self.name] = round(time.time() - self.t, 1)
        print(f"[timing] {self.name}: {TIMINGS[self.name]} s", flush=True)


def patch(text: str, old: str, new: str, count: int = 1) -> str:
    found = text.count(old)
    if found != count:
        raise SystemExit(f"patch anchor found {found} times (expected {count}): {old!r}")
    return text.replace(old, new)


def build_client(repo: Path) -> None:
    npm = "npm.cmd" if os.name == "nt" else "npm"
    frontend = repo / "frontend"
    env = dict(os.environ, npm_config_audit="false", npm_config_fund="false")
    run([npm, "ci", "--ignore-scripts", "--no-audit", "--no-fund"], cwd=frontend, env=env)
    run([npm, "run", "build"], cwd=frontend, env=env)
    run(["node", "scripts/asset-manifest.mjs", "dist", "--package"], cwd=frontend, env=env)


def compile_deterministic(python: Path, targets: list[Path], strip: Path) -> dict:
    removed = 0
    for target in targets:
        for cache in list(target.rglob("__pycache__")):
            if cache.is_dir():
                shutil.rmtree(cache)
                removed += 1
    env = dict(os.environ, PYTHONNOUSERSITE="1")
    env.pop("PYTHONDONTWRITEBYTECODE", None)
    code = run(
        [python, "-m", "compileall", "-q", "-f", "-j", "0", "-s", strip, "--invalidation-mode", "unchecked-hash", *targets],
        env=env,
        check=False,
    )
    pyc = sum(1 for t in targets for _ in t.rglob("*.pyc"))
    return {"removed_pycache_dirs": removed, "compileall_exit": code, "pyc_files": pyc}


def file_stats(root: Path) -> dict:
    files = total = 0
    for base, _dirs, names in os.walk(root):
        for name in names:
            path = os.path.join(base, name)
            if os.path.islink(path):
                continue
            files += 1
            total += os.path.getsize(path)
    return {"files": files, "bytes": total}


def build_windows(args: argparse.Namespace, repo: Path, out: Path) -> dict:
    info: dict = {}
    script = (repo / "installer" / "build_installer.ps1").read_text(encoding="utf-8")
    script = patch(script, "& $PythonExe $GetPipPath --no-warn-script-location", "& $PythonExe $GetPipPath --no-compile --no-warn-script-location")
    script = patch(script, "& $PythonExe -m pip install --no-warn-script-location", "& $PythonExe -m pip install --no-compile --no-warn-script-location", count=2)
    stop = (
        "Set-Content -Path (Join-Path $BuildDir 'trial-client-stage.txt') -Value $ClientAssetStage -Encoding utf8\n"
        "Write-Host 'TRIAL: stopping before Inno Setup'\n"
        "return\n"
    )
    script = patch(script, '$DistDir = Join-Path (Join-Path $PSScriptRoot "..") "dist"', stop + '$DistDir = Join-Path (Join-Path $PSScriptRoot "..") "dist"')
    trial_script = repo / "installer" / "build_installer.trial.ps1"
    trial_script.write_text(script, encoding="utf-8")
    with timed("tag_build_script"):
        run(["pwsh", "-NoProfile", "-File", trial_script, "-PythonVersion", args.python_version], cwd=repo)

    build = repo / "installer" / "build"
    client_stage = Path((build / "trial-client-stage.txt").read_text(encoding="utf-8-sig").strip())
    with timed("stage"):
        out.mkdir(parents=True, exist_ok=True)
        shutil.move(str(build / "python"), str(out / "python"))
        app = out / "app"
        manifest_py = repo / "scripts" / "app_payload_manifest.py"

        def category(name: str) -> list[str]:
            res = subprocess.run(  # noqa: S603
                [sys.executable, str(manifest_py), "--project-root", str(repo), "--category", name],
                check=True,
                capture_output=True,
                text=True,
            )
            return [line.strip() for line in res.stdout.splitlines() if line.strip()]

        for rel in category("root_python_files") + category("root_files") + category("runtime_script_files"):
            dest = app / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(repo / rel, dest)
        # Same excludes as installer/row_bot_setup.iss [Files] for src\row_bot.
        exclude_dirs = {"__pycache__", "node_modules", ".pytest_cache", "tests", "test", "test-results"}

        def ignore(directory: str, names: list[str]) -> set[str]:
            skipped = set()
            for name in names:
                full = Path(directory) / name
                if full.is_dir() and name in exclude_dirs:
                    skipped.add(name)
                elif name.endswith((".pyc", ".test.js", ".spec.js")) or ".bak" in name:
                    skipped.add(name)
            if Path(directory).as_posix().endswith("row_bot/static") and "client-v2" in names:
                skipped.add("client-v2")
            return skipped

        for rel in category("payload_dirs"):
            shutil.copytree(repo / rel, app / rel, ignore=ignore)
        shutil.copytree(client_stage, app / "src" / "row_bot" / "static" / "client-v2")
        for rel in category("asset_dirs"):
            shutil.copytree(repo / rel, app / rel)
    with timed("compileall"):
        info["compile"] = compile_deterministic(out / "python" / "python.exe", [out / "python" / "Lib", out / "app"], strip=out)
    return info


def build_macos(args: argparse.Namespace, repo: Path, out: Path) -> dict:
    info: dict = {}
    script = (repo / "installer" / "build_mac_app.sh").read_text(encoding="utf-8")
    script = patch(
        script,
        '"$PYTHON_PREFIX/bin/python3" -m pip install --upgrade pip setuptools wheel --quiet',
        '"$PYTHON_PREFIX/bin/python3" -m pip install --no-compile --upgrade pip setuptools wheel --quiet',
    )
    script = patch(
        script,
        '"$PYTHON_PREFIX/bin/python3" -m pip install -r "$PROJECT_DIR/requirements.txt" --quiet',
        '"$PYTHON_PREFIX/bin/python3" -m pip install --no-compile -r "$PROJECT_DIR/requirements.txt" --quiet',
    )
    compile_block = (
        "# TRIAL: deterministic, path-independent bytecode\n"
        'find "$APP_BUNDLE" -type d -name __pycache__ -prune -exec rm -rf {} +\n'
        'PYTHONNOUSERSITE=1 "$PYTHON_PREFIX/bin/python3" -m compileall -q -f -j 0 -s "$RESOURCES" '
        '--invalidation-mode unchecked-hash "$PYTHON_PREFIX/lib" "$APP_SRC" '
        '|| echo "TRIAL: compileall reported errors ($?)"\n'
    )
    script = patch(script, 'info "[6/6] Packaging..."\n', compile_block + 'info "[6/6] Packaging..."\n')
    script = patch(script, 'COMPONENT_PKG="$BUILD_DIR/Row-Bot-component.pkg"\n', 'echo "TRIAL: stopping before pkg"\nexit 0\nCOMPONENT_PKG="$BUILD_DIR/Row-Bot-component.pkg"\n')
    trial_script = repo / "installer" / "build_mac_app.trial.sh"
    trial_script.write_text(script, encoding="utf-8")
    trial_script.chmod(0o755)
    env = dict(os.environ, BUNDLE_PLAYWRIGHT="0", CODESIGN_IDENTITY="", PKG_SIGN_IDENTITY="")
    with timed("tag_build_script"):
        run(["bash", trial_script, args.version], cwd=repo, env=env)
    bundle = repo / "installer" / "build" / "mac" / "Row-Bot.app"
    info["compile"] = {"pyc_files": sum(1 for _ in bundle.rglob("*.pyc"))}
    out.mkdir(parents=True, exist_ok=True)
    shutil.move(str(bundle), str(out / "Row-Bot.app"))
    return info


def build_linux(args: argparse.Namespace, repo: Path, out: Path) -> dict:
    info: dict = {}
    script = (repo / "installer" / "build_linux_app.sh").read_text(encoding="utf-8")
    script = patch(
        script,
        '"$PYTHON_PREFIX/bin/python3" -m pip install --upgrade pip setuptools wheel --quiet',
        '"$PYTHON_PREFIX/bin/python3" -m pip install --no-compile --upgrade pip setuptools wheel --quiet',
    )
    script = patch(
        script,
        '"$PYTHON_PREFIX/bin/python3" -m pip install -r "$PROJECT_DIR/requirements.txt" --quiet',
        '"$PYTHON_PREFIX/bin/python3" -m pip install --no-compile -r "$PROJECT_DIR/requirements.txt" --quiet',
    )
    stop = (
        "# TRIAL: deterministic, path-independent bytecode; stop before the tarball\n"
        'find "$PACKAGE_ROOT" -type d -name __pycache__ -prune -exec rm -rf {} +\n'
        'PYTHONNOUSERSITE=1 "$PYTHON_PREFIX/bin/python3" -m compileall -q -f -j 0 -s "$PACKAGE_ROOT" '
        '--invalidation-mode unchecked-hash "$PYTHON_PREFIX/lib" "$APP_SRC" '
        '|| echo "TRIAL: compileall reported errors ($?)"\n'
        'echo "TRIAL: stopping before the tarball"\n'
        "exit 0\n"
    )
    script = patch(script, 'info "[6/6] Creating tarball..."\n', stop + 'info "[6/6] Creating tarball..."\n')
    trial_script = repo / "installer" / "build_linux_app.trial.sh"
    trial_script.write_text(script, encoding="utf-8")
    trial_script.chmod(0o755)
    with timed("tag_build_script"):
        run(["bash", trial_script, args.version], cwd=repo, env=dict(os.environ, BUNDLE_PLAYWRIGHT="0"))
    roots = sorted((repo / "installer" / "build" / "linux").glob(f"Row-Bot-{args.version}-Linux-*"))
    roots = [r for r in roots if r.is_dir()]
    if len(roots) != 1:
        raise SystemExit(f"package root not found: {roots}")
    info["compile"] = {"pyc_files": sum(1 for _ in roots[0].rglob("*.pyc"))}
    out.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(roots[0]), str(out))
    return info


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--os", choices=["windows", "macos", "linux"], required=True)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--python-version", default="3.13.2")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    repo = args.repo.resolve()
    out = args.out.resolve()
    if out.exists():
        raise SystemExit(f"output exists: {out}")
    with timed("client_build"):
        build_client(repo)
    builder = {"windows": build_windows, "macos": build_macos, "linux": build_linux}[args.os]
    info = builder(args, repo, out)
    payload_root = out / "Row-Bot.app" if args.os == "macos" else out
    report = {"os": args.os, "version": args.version, "out": str(out), **file_stats(payload_root), "timings": TIMINGS, **info}
    if args.os != "macos":
        report["python_dir"] = file_stats(out / "python")
        report["app_dir"] = file_stats(out / "app")
    else:
        resources = out / "Row-Bot.app" / "Contents" / "Resources"
        report["python_dir"] = file_stats(resources / "python")
        report["app_dir"] = file_stats(resources / "app")
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
