"""Build the platform-independent update parts for one Row-Bot tag (Phase 1 spike).

From a checkout of the tag:
  * code archive:   root files, src/row_bot incl. the built client, bundled_skills,
                    tool_guides, scripts/verify_runtime_dependencies.py
  * assets archive: static/ and sounds/
Each as tar.xz (used for staging), with tar.gz and zip sizes for comparison.

Plus hashed lock exports (requirements with hashes, and pylock.toml) for each
--export-tag, the changed libraries per platform between two locks, wheel
availability, and an update-manifest prototype (row-bot-update-<v>.json).

  python build_parts.py --repo rb --version 5.1.0 --git-dir . --export-tags v4.5.0 v5.0.0 v5.1.0 --out parts
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import time
import tomllib
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import engine  # noqa: E402
import wheelcheck  # noqa: E402

EXCLUDE_DIRS = {"__pycache__", "node_modules", ".pytest_cache", "tests", "test", "test-results"}


def run(cmd: list, cwd: Path | None = None, env: dict | None = None) -> None:
    print("+", " ".join(str(c) for c in cmd), flush=True)
    subprocess.run([str(c) for c in cmd], cwd=cwd, env=env, check=True)  # noqa: S603


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def manifest_category(repo: Path, name: str) -> list[str]:
    out = subprocess.run(
        [sys.executable, str(repo / "scripts" / "app_payload_manifest.py"), "--project-root", str(repo), "--category", name],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [line.strip() for line in out.splitlines() if line.strip()]


def build_client(repo: Path) -> None:
    frontend = repo / "frontend"
    env = dict(os.environ, npm_config_audit="false", npm_config_fund="false")
    run(["npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund"], cwd=frontend, env=env)
    run(["npm", "run", "build"], cwd=frontend, env=env)
    run(["node", "scripts/asset-manifest.mjs", "dist", "--package"], cwd=frontend, env=env)


def assemble_app(repo: Path, app: Path) -> None:
    """Same inventory and excludes as installer/build_linux_app.sh."""
    for rel in manifest_category(repo, "root_python_files") + manifest_category(repo, "root_files") + manifest_category(repo, "runtime_script_files"):
        dest = app / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(repo / rel, dest)

    def ignore_code(directory: str, names: list[str]) -> set[str]:
        skipped = set()
        for name in names:
            full = Path(directory) / name
            if full.is_dir() and name in EXCLUDE_DIRS:
                skipped.add(name)
            elif name.endswith((".pyc", ".test.js", ".spec.js")) or ".bak" in name:
                skipped.add(name)
        if Path(directory).as_posix().endswith("row_bot/static") and "client-v2" in names:
            skipped.add("client-v2")
        return skipped

    for rel in manifest_category(repo, "payload_dirs"):
        shutil.copytree(repo / rel, app / rel, ignore=ignore_code)
    run(
        ["node", repo / "frontend" / "scripts" / "asset-manifest.mjs", repo / "frontend" / "dist", "--package-dir",
         app / "src" / "row_bot" / "static" / "client-v2"],
    )
    for rel in manifest_category(repo, "asset_dirs"):
        shutil.copytree(repo / rel, app / rel, ignore=lambda _d, names: {n for n in names if n.endswith(".pyc")})


def _normalize(info: tarfile.TarInfo) -> tarfile.TarInfo:
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    info.mtime = 0
    return info


def members(app: Path, include) -> list[Path]:  # noqa: ANN001
    return sorted(p for p in app.rglob("*") if p.is_file() and include(p.relative_to(app).as_posix()))


def write_archives(app: Path, files: list[Path], stem: Path) -> dict:
    out: dict = {"files": len(files), "uncompressed_bytes": sum(p.stat().st_size for p in files), "formats": {}}
    for fmt, mode in (("tar.xz", "w:xz"), ("tar.gz", "w:gz")):
        path = stem.with_name(f"{stem.name}.{fmt}")
        started = time.time()
        kwargs = {"preset": 9} if fmt == "tar.xz" else {"compresslevel": 9}
        with tarfile.open(path, mode, format=tarfile.PAX_FORMAT, **kwargs) as tar:
            for file in files:
                tar.add(file, arcname=file.relative_to(app).as_posix(), filter=_normalize)
        out["formats"][fmt] = {"file": path.name, "bytes": path.stat().st_size, "sha256": sha256(path), "seconds": round(time.time() - started, 1)}
    path = stem.with_name(f"{stem.name}.zip")
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for file in files:
            zf.write(file, file.relative_to(app).as_posix())
    out["formats"]["zip"] = {"file": path.name, "bytes": path.stat().st_size}
    path.unlink()  # size only
    tgz = stem.with_name(f"{stem.name}.tar.gz")
    tgz.unlink()  # size only
    return out


def export_lock(git_dir: Path, tag: str, out: Path, uv: str) -> dict:
    version = tag.lstrip("v")
    work = out / f"export-{version}"
    work.mkdir(parents=True, exist_ok=True)
    for name in ("pyproject.toml", "uv.lock"):
        data = subprocess.run(["git", "-C", str(git_dir), "show", f"{tag}:{name}"], check=True, capture_output=True).stdout
        (work / name).write_bytes(data)
    env = dict(os.environ, UV_NO_CONFIG="1")
    base = [uv, "export", "--locked", "--all-extras", "--no-dev", "--no-emit-project"]
    run([*base, "--output-file", out / f"requirements-hashed-{version}.txt"], cwd=work, env=env)
    run([*base, "--format", "pylock.toml", "--output-file", out / f"pylock-{version}.toml"], cwd=work, env=env)
    lock_sha = sha256(work / "uv.lock")
    shutil.rmtree(work)
    req = out / f"requirements-hashed-{version}.txt"
    return {
        "tag": tag,
        "lock_sha256": lock_sha,
        "requirements_hashed": {"file": req.name, "bytes": req.stat().st_size, "packages": sum(1 for l in req.read_text().splitlines() if "==" in l and not l.startswith(" "))},
        "pylock": {"file": f"pylock-{version}.toml", "bytes": (out / f"pylock-{version}.toml").stat().st_size},
    }


def lock_changes(old: Path, new: Path) -> dict:
    """Packages whose version differs between two pylock exports, per target."""
    from packaging.markers import Marker
    from packaging.utils import parse_wheel_filename
    from urllib.parse import unquote

    def applicable(pylock: Path, env: dict) -> dict[str, dict]:
        out = {}
        for pkg in tomllib.loads(pylock.read_text(encoding="utf-8"))["packages"]:
            marker = pkg.get("marker")
            if marker and not Marker(marker).evaluate(env):
                continue
            out[engine.canon(pkg["name"])] = pkg
        return out

    result = {}
    for target, spec in wheelcheck.TARGETS.items():
        tags = set(spec["tags"](spec["versions"][-1]))
        a, b = applicable(old, spec["env"]), applicable(new, spec["env"])
        rows = []
        for name in sorted(set(a) | set(b)):
            va = a.get(name, {}).get("version")
            vb = b.get(name, {}).get("version")
            if va == vb:
                continue
            size = None
            has_wheel = False
            if vb:
                matches = [w for w in b[name].get("wheels") or [] if parse_wheel_filename(unquote(w["url"].rsplit("/", 1)[-1]))[3] & tags]
                has_wheel = bool(matches)
                known = [w["size"] for w in matches if w.get("size")]
                size = min(known) if known else None  # the most specific wheel is usually the smallest
            rows.append({"name": name, "from": va, "to": vb, "wheel_bytes": size, "has_wheel": has_wheel})
        result[target] = {
            "changed": len(rows),
            "download_bytes_known": sum(r["wheel_bytes"] or 0 for r in rows),
            "size_unknown_in_lock": [r["name"] for r in rows if r["to"] and r["has_wheel"] and r["wheel_bytes"] is None],
            "rows": rows,
        }
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True, help="checkout of the tag to package")
    parser.add_argument("--version", required=True)
    parser.add_argument("--git-dir", type=Path, required=True, help="repository with all tags, for lock exports")
    parser.add_argument("--export-tags", nargs="+", required=True)
    parser.add_argument("--base-version", default="5.0.0")
    parser.add_argument("--big-jump-version", default="4.5.0")
    parser.add_argument("--uv", default="uv")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--skip-client", action="store_true")
    args = parser.parse_args()
    repo = args.repo.resolve()
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    report: dict = {"version": args.version, "timings": {}}

    started = time.time()
    if not args.skip_client:
        build_client(repo)
    report["timings"]["client_build"] = round(time.time() - started, 1)

    stage = out.parent / "parts-stage"
    if stage.exists():
        shutil.rmtree(stage)
    app = stage / "app"
    app.mkdir(parents=True)
    started = time.time()
    assemble_app(repo, app)
    report["timings"]["assemble"] = round(time.time() - started, 1)

    is_asset = lambda rel: rel.split("/", 1)[0] in engine.ASSET_DIRS  # noqa: E731
    started = time.time()
    code = write_archives(app, members(app, lambda rel: not is_asset(rel)), out / f"row-bot-code-{args.version}")
    assets = write_archives(app, members(app, is_asset), out / f"row-bot-assets-{args.version}")
    report["timings"]["archives"] = round(time.time() - started, 1)
    report["code"] = code
    report["assets"] = assets
    tops: dict[str, dict] = {}
    for path in members(app, lambda rel: True):
        rel = path.relative_to(app).as_posix()
        top = "/".join(rel.split("/")[:3]) if rel.startswith("src/row_bot/static/client-v2") else rel.split("/")[0]
        row = tops.setdefault(top, {"files": 0, "bytes": 0})
        row["files"] += 1
        row["bytes"] += path.stat().st_size
    report["app_inventory"] = dict(sorted(tops.items(), key=lambda kv: -kv[1]["bytes"]))

    exports = {}
    for tag in args.export_tags:
        exports[tag.lstrip("v")] = export_lock(args.git_dir.resolve(), tag, out, args.uv)
    report["exports"] = exports

    pylock_new = out / f"pylock-{args.version}.toml"
    report["changes_from_base"] = lock_changes(out / f"pylock-{args.base_version}.toml", pylock_new)
    if (out / f"pylock-{args.big_jump_version}.toml").exists():
        report["big_jump_from_base"] = lock_changes(out / f"pylock-{args.base_version}.toml", out / f"pylock-{args.big_jump_version}.toml")
    wheels = wheelcheck.check(pylock_new)
    (out / "wheels.json").write_text(json.dumps(wheels, indent=2), encoding="utf-8")
    report["wheel_availability"] = {
        t: {k: v for k, v in info.items() if k != "rows"} for t, info in wheels["targets"].items()
    }

    xz = lambda part: part["formats"]["tar.xz"]  # noqa: E731
    update = {
        "schema": 1,
        "version": args.version,
        "min_source_version": args.base_version,
        "code": {"file": xz(code)["file"], "size": xz(code)["bytes"], "sha256": xz(code)["sha256"]},
        "assets": {
            "file": xz(assets)["file"],
            "size": xz(assets)["bytes"],
            "sha256": xz(assets)["sha256"],
            "content_sha256": engine.content_hash(app, engine.ASSET_DIRS),
        },
        "lock_sha256": exports[args.version]["lock_sha256"],
        "lock_exports": {"requirements_hashed": f"requirements-hashed-{args.version}.txt", "pylock": f"pylock-{args.version}.toml"},
        "python": {"windows-x64": "3.13.2", "macos-arm64": "3.12.13", "linux-x86_64": "3.12.13"},
    }
    (out / f"row-bot-update-{args.version}.json").write_text(json.dumps(update, indent=2), encoding="utf-8")
    report["update_json_bytes"] = (out / f"row-bot-update-{args.version}.json").stat().st_size
    (out / "parts-report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    shutil.rmtree(stage)
    print(json.dumps({k: v for k, v in report.items() if k not in {"changes_from_base", "big_jump_from_base"}}, indent=2)[:6000])
    for target, info in report["changes_from_base"].items():
        print(target, info["changed"], "changed,", info["download_bytes_known"], "wheel bytes (known):", [f"{r['name']} {r['from']}->{r['to']}" for r in info["rows"]])


if __name__ == "__main__":
    main()
