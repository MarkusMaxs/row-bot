"""Build one Velopack trial payload from a checked-out Row-Bot tag (Phase 0 spike).

Runs the tag's own installer build script, patched only to:
  * stop before Inno Setup (Windows) or pkgbuild (macOS),
  * install the pinned velopack wheel next to the locked requirements,
  * for the deterministic variant (b): pip install --no-compile, then
    python -m compileall --invalidation-mode unchecked-hash.
Then stages the Velopack pack directory and writes files.json.

Usage (CI only):
  python build_payload.py --os windows|macos --repo rb --variant a|b \
      --version 5.0.0 --out <dir> --velopack-version 1.2.161 [--stub Row-Bot.exe]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
TIMINGS: dict[str, float] = {}


def run(cmd: list[str], cwd: Path | None = None, env: dict | None = None, check: bool = True) -> int:
    print("+", " ".join(str(c) for c in cmd), flush=True)
    proc = subprocess.run([str(c) for c in cmd], cwd=cwd, env=env)  # noqa: S603 - CI build tool
    if check and proc.returncode != 0:
        raise SystemExit(f"command failed ({proc.returncode}): {cmd}")
    return proc.returncode


def timed(name: str):
    class _T:
        def __enter__(self):
            self.t = time.time()

        def __exit__(self, *exc):
            TIMINGS[name] = round(time.time() - self.t, 1)
            print(f"[timing] {name}: {TIMINGS[name]} s", flush=True)

    return _T()


def patch(text: str, old: str, new: str, count: int = 1) -> str:
    found = text.count(old)
    if found != count:
        raise SystemExit(f"patch anchor found {found} times (expected {count}): {old!r}")
    return text.replace(old, new)


def build_client(repo: Path) -> None:
    npm = "npm.cmd" if os.name == "nt" else "npm"
    node = "node"
    frontend = repo / "frontend"
    env = dict(os.environ, npm_config_audit="false", npm_config_fund="false")
    run([npm, "ci", "--ignore-scripts", "--no-audit", "--no-fund"], cwd=frontend, env=env)
    run([npm, "run", "build"], cwd=frontend, env=env)
    run([node, "scripts/asset-manifest.mjs", "dist", "--package"], cwd=frontend, env=env)


def compile_deterministic(python: Path, targets: list[Path], strip: Path | None = None) -> dict:
    removed = 0
    for target in targets:
        for cache in list(target.rglob("__pycache__")):
            if cache.is_dir():
                shutil.rmtree(cache)
                removed += 1
    env = dict(os.environ, PYTHONNOUSERSITE="1")
    env.pop("PYTHONDONTWRITEBYTECODE", None)
    # co_filename is baked into each .pyc; strip the build folder so the bytes do
    # not depend on where the payload was staged (importlib fixes it up on load).
    strip_args = ["-s", str(strip)] if strip else []
    code = run(
        [python, "-m", "compileall", "-q", "-f", "-j", "0", *strip_args, "--invalidation-mode", "unchecked-hash", *targets],
        env=env,
        check=False,
    )
    pyc = sum(1 for t in targets for _ in t.rglob("*.pyc"))
    return {"removed_pycache_dirs": removed, "compileall_exit": code, "pyc_files": pyc}


def manifest(root: Path) -> dict:
    files = []
    total = 0
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if path.is_symlink():
            files.append({"path": rel, "link": os.readlink(path)})
            continue
        if not path.is_file():
            continue
        data = path.read_bytes()
        mode = path.stat().st_mode
        total += len(data)
        files.append(
            {
                "path": rel,
                "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "exec": bool(mode & stat.S_IXUSR) if os.name != "nt" else None,
            }
        )
    return {"root": str(root), "count": len(files), "bytes": total, "files": files}


# --------------------------------------------------------------------------- Windows


def build_windows(args: argparse.Namespace, repo: Path, out: Path) -> dict:
    info: dict = {}
    script = (repo / "installer" / "build_installer.ps1").read_text(encoding="utf-8")
    nc = "--no-compile " if args.variant == "b" else ""
    script = patch(
        script,
        "& $PythonExe $GetPipPath --no-warn-script-location",
        f"& $PythonExe $GetPipPath {nc}--no-warn-script-location",
    )
    script = patch(
        script,
        "& $PythonExe -m pip install --no-warn-script-location",
        f"& $PythonExe -m pip install {nc}--no-warn-script-location",
        count=2,
    )
    stop = (
        "& $PythonExe -m pip install " + nc + "--no-warn-script-location "
        f"velopack=={args.velopack_version}\n"
        "if ($LASTEXITCODE -ne 0) { throw 'velopack wheel install failed' }\n"
        "Set-Content -Path (Join-Path $BuildDir 'trial-client-stage.txt') -Value $ClientAssetStage -Encoding utf8\n"
        "Write-Host 'TRIAL: stopping before Inno Setup'\n"
        "return\n"
    )
    script = patch(
        script,
        '$DistDir = Join-Path (Join-Path $PSScriptRoot "..") "dist"',
        stop + '$DistDir = Join-Path (Join-Path $PSScriptRoot "..") "dist"',
    )
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

        for rel in category("root_python_files") + category("root_files"):
            (app / Path(rel).name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(repo / rel, app / Path(rel).name)
        for rel in category("runtime_script_files"):
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
        os.replace(app / "launcher.py", app / "launcher_real.py")
        shutil.copy2(HERE / "trial_launcher.py", app / "launcher.py")
        shutil.copy2(args.stub, out / "Row-Bot.exe")

    if args.variant == "b":
        with timed("compileall"):
            info["compile"] = compile_deterministic(
                out / "python" / "python.exe", [out / "python" / "Lib", out / "app"], strip=out
            )
    return info


# --------------------------------------------------------------------------- macOS


def build_macos(args: argparse.Namespace, repo: Path, out: Path) -> dict:
    info: dict = {}
    script = (repo / "installer" / "build_mac_app.sh").read_text(encoding="utf-8")
    nc = " --no-compile" if args.variant == "b" else ""
    script = patch(
        script,
        '"$PYTHON_PREFIX/bin/python3" -m pip install --upgrade pip setuptools wheel --quiet',
        f'"$PYTHON_PREFIX/bin/python3" -m pip install{nc} --upgrade pip setuptools wheel --quiet',
    )
    script = patch(
        script,
        '"$PYTHON_PREFIX/bin/python3" -m pip install -r "$PROJECT_DIR/requirements.txt" --quiet 2>&1 | tail -5\n',
        f'"$PYTHON_PREFIX/bin/python3" -m pip install{nc} -r "$PROJECT_DIR/requirements.txt" --quiet 2>&1 | tail -5\n'
        f'"$PYTHON_PREFIX/bin/python3" -m pip install{nc} "velopack=={args.velopack_version}"\n',
    )
    if args.variant == "b":
        compile_block = (
            '# TRIAL: deterministic bytecode (variant b)\n'
            'find "$APP_BUNDLE" -type d -name __pycache__ -prune -exec rm -rf {} +\n'
            'PYTHONNOUSERSITE=1 "$PYTHON_PREFIX/bin/python3" -m compileall -q -f -j 0 '
            '--invalidation-mode unchecked-hash "$PYTHON_PREFIX/lib" "$APP_SRC" '
            '|| echo "TRIAL: compileall reported errors ($?)"\n'
        )
        script = patch(script, 'info "[6/6] Packaging..."\n', compile_block + 'info "[6/6] Packaging..."\n')
    script = patch(
        script,
        'COMPONENT_PKG="$BUILD_DIR/Row-Bot-component.pkg"\n',
        'echo "TRIAL: stopping before pkg"\nexit 0\nCOMPONENT_PKG="$BUILD_DIR/Row-Bot-component.pkg"\n',
    )
    trial_script = repo / "installer" / "build_mac_app.trial.sh"
    trial_script.write_text(script, encoding="utf-8")
    trial_script.chmod(0o755)
    env = dict(os.environ, BUNDLE_PLAYWRIGHT="0", CODESIGN_IDENTITY="", PKG_SIGN_IDENTITY="")
    with timed("tag_build_script"):
        run(["bash", trial_script, args.version], cwd=repo, env=env)

    bundle = repo / "installer" / "build" / "mac" / "Row-Bot.app"
    app = bundle / "Contents" / "Resources" / "app"
    os.replace(app / "launcher.py", app / "launcher_real.py")
    shutil.copy2(HERE / "trial_launcher.py", app / "launcher.py")
    if args.variant == "b":
        pyc = sum(1 for _ in bundle.rglob("*.pyc"))
        info["compile"] = {"pyc_files": pyc}
    else:
        info["pyc_files_left_by_build"] = sum(1 for _ in bundle.rglob("*.pyc"))
    out.mkdir(parents=True, exist_ok=True)
    shutil.move(str(bundle), str(out / "Row-Bot.app"))
    return info


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--os", choices=["windows", "macos"], required=True)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--variant", choices=["a", "b"], required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--velopack-version", required=True)
    parser.add_argument("--python-version", default="3.13.2")
    parser.add_argument("--stub", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    repo = args.repo.resolve()
    out = args.out.resolve()
    if out.exists():
        raise SystemExit(f"output exists: {out}")
    with timed("client_build"):
        build_client(repo)
    info = build_windows(args, repo, out) if args.os == "windows" else build_macos(args, repo, out)
    payload_root = out if args.os == "windows" else out / "Row-Bot.app"
    with timed("manifest"):
        files = manifest(payload_root)
    report = {
        "os": args.os,
        "variant": args.variant,
        "version": args.version,
        "out": str(out),
        "file_count": files["count"],
        "bytes": files["bytes"],
        "timings": TIMINGS,
        **info,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    (args.report.parent / f"files-{args.os}-{args.variant}-{args.version}.json").write_text(
        json.dumps(files), encoding="utf-8"
    )
    args.report.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
