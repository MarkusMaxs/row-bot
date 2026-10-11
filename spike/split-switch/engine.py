"""Split-and-switch staging engine prototype (Phase 1 spike, throwaway).

Stages version N+1 next to the running version N, the way PLAN.md describes:

  1. download the code and assets archives and check their SHA-256
  2. clone N's python/ (hard links, APFS clonefile, or a plain copy)
  3. sync the clone's libraries against N+1's lock (uv or pip, wheels only,
     every file hash-checked), downloading only what changed
  4. unpack code and assets, compile bytecode for what changed
  5. self-test with the staged Python, isolated from the running app
  6. rename <new>.staging -> <new>, write version.json, set state.ready

Any failure removes the staging folder and leaves state.json untouched.
Everything is timed; the caller gets a JSON-able report.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import time
import tomllib
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

IS_WIN = os.name == "nt"
IS_MAC = sys.platform == "darwin"
KEEP_DISTS = {"pip", "setuptools", "wheel"}
PYPI = "https://pypi.org/simple"
TORCH_INDEX = "https://download.pytorch.org/whl/cpu"
ALLOWED_HOSTS = {"pypi.org", "files.pythonhosted.org", "download.pytorch.org", "127.0.0.1"}


ASSET_DIRS = ("static", "sounds")


def content_hash(app: Path, subdirs: tuple[str, ...] = ASSET_DIRS) -> str:
    """SHA-256 over (relative path, file SHA-256) of every file under app/<subdirs>."""
    outer = hashlib.sha256()
    for sub in subdirs:
        base = app / sub
        if not base.exists():
            continue
        for path in sorted(p for p in base.rglob("*") if p.is_file()):
            outer.update(path.relative_to(app).as_posix().encode() + b"\0")
            outer.update(hashlib.sha256(path.read_bytes()).digest())
    return outer.hexdigest()


class StageError(Exception):
    def __init__(self, step: str, message: str) -> None:
        super().__init__(f"{step}: {message}")
        self.step = step
        self.message = message


def canon(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# --------------------------------------------------------------------------- state.json


def read_state(root: Path) -> dict | None:
    try:
        data = json.loads((root / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("schema") == 1 else None


def write_state(root: Path, state: dict) -> None:
    """Atomic: write a temp file next to state.json, flush, then rename over it."""
    root.mkdir(parents=True, exist_ok=True)
    tmp = root / f".state.{os.getpid()}.{time.time_ns()}.tmp"
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    for attempt in range(20):
        try:
            os.replace(tmp, root / "state.json")
            return
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(0.05)


def new_state(active: str) -> dict:
    return {
        "schema": 1,
        "active": active,
        "previous": None,
        "ready": None,
        "pending_verification": False,
        "failed_starts": 0,
    }


def launch_decision(state: dict, max_failed: int = 2) -> tuple[dict, str | None]:
    """The native launcher's rule, applied on every launch.

    While pending_verification is set each launch counts as a failed start
    (the Python launcher clears the flag once the server is ready). After
    `max_failed` such starts, switch back to `previous`. Returns the updated
    state and a rollback reason, if one happened.
    """
    state = dict(state)
    if not state.get("pending_verification"):
        return state, None
    if int(state.get("failed_starts") or 0) >= max_failed and state.get("previous"):
        bad = state["active"]
        state["active"] = state["previous"]
        state["previous"] = None
        state["pending_verification"] = False
        state["failed_starts"] = 0
        state["last_rollback"] = {"from": bad, "to": state["active"], "reason": f"{max_failed} failed starts", "at": now_iso()}
        return state, f"rolled back from {bad} after {max_failed} failed starts"
    state["failed_starts"] = int(state.get("failed_starts") or 0) + 1
    return state, None


# --------------------------------------------------------------------------- layout


class Layout:
    """R/versions/<v>/{app,python} (Windows, macOS) or R/releases/<v> (Linux).

    On macOS the bundle's Contents/Resources holds the seed version.
    """

    def __init__(self, root: Path, kind: str, seed: Path | None = None, seed_version: str | None = None) -> None:
        self.root = root
        self.kind = kind
        self.seed = seed
        self.seed_version = seed_version
        self.versions = root / ("releases" if kind == "linux" else "versions")

    def version_root(self, version: str) -> Path:
        candidate = self.versions / version
        if self.kind == "macos" and self.seed is not None and version == self.seed_version and not candidate.exists():
            return self.seed
        return candidate

    @staticmethod
    def python_exe(vroot: Path, gui: bool = False) -> Path:
        if IS_WIN:
            return vroot / "python" / ("pythonw.exe" if gui else "python.exe")
        return vroot / "python" / "bin" / "python3"

    @staticmethod
    def site_packages(vroot: Path) -> Path:
        if IS_WIN:
            return vroot / "python" / "Lib" / "site-packages"
        found = sorted((vroot / "python" / "lib").glob("python3.*/site-packages"))
        if not found:
            raise FileNotFoundError(f"no site-packages under {vroot}")
        return found[0]


def runtime_env(vroot: Path, data_dir: Path | None = None) -> dict:
    """The environment the native launchers set (launch_row_bot.bat / tray host)."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PYTHON", "ROW_BOT_", "VIRTUAL_ENV", "UV_", "PIP_"))}
    env.update({"PYTHONNOUSERSITE": "1", "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1", "ROW_BOT_INSTALL_ROOT": str(vroot)})
    py = vroot / "python"
    if IS_WIN:
        env["TCL_LIBRARY"] = str(py / "tcl" / "tcl8.6")
        env["TK_LIBRARY"] = str(py / "tcl" / "tk8.6")
        env["PATH"] = f"{py / 'Scripts'};{py};{os.environ.get('PATH', '')}"
    browsers = py / "playwright-browsers"
    if browsers.is_dir():
        env["PLAYWRIGHT_BROWSERS_PATH"] = str(browsers)
    if data_dir is not None:
        env["ROW_BOT_DATA_DIR"] = str(data_dir)
    return env


# --------------------------------------------------------------------------- tree helpers


def tree_manifest(root: Path, jobs: int = 8) -> dict:
    """rel path -> [size, sha256, inode] for every regular file; symlinks recorded as links."""
    files: list[Path] = []
    links: dict[str, str] = {}
    for base, dirs, names in os.walk(root):
        for name in list(dirs):
            full = Path(base) / name
            if full.is_symlink():
                links[full.relative_to(root).as_posix()] = os.readlink(full)
                dirs.remove(name)
        for name in names:
            full = Path(base) / name
            if full.is_symlink():
                links[full.relative_to(root).as_posix()] = os.readlink(full)
            else:
                files.append(full)

    def one(path: Path) -> tuple[str, list]:
        st = path.stat()
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
        return path.relative_to(root).as_posix(), [st.st_size, digest.hexdigest(), st.st_ino]

    with ThreadPoolExecutor(max_workers=jobs) as pool:
        entries = dict(pool.map(one, files))
    return {"files": entries, "links": links}


def compare_manifests(before: dict, after: dict) -> dict:
    b, a = before["files"], after["files"]
    changed = [p for p in b if p in a and (a[p][0], a[p][1]) != (b[p][0], b[p][1])]
    removed = [p for p in b if p not in a]
    added = [p for p in a if p not in b]
    inode_changed = [p for p in b if p in a and a[p][2] != b[p][2]]
    return {
        "files_before": len(b),
        "files_after": len(a),
        "content_changed": len(changed),
        "removed": len(removed),
        "added": len(added),
        "inode_changed": len(inode_changed),
        "examples": {"changed": changed[:20], "removed": removed[:20], "added": added[:20], "inode_changed": inode_changed[:20]},
        "links_changed": sorted(set(before["links"].items()) ^ set(after["links"].items()))[:20],
    }


def dir_stats(root: Path, exclude_inodes: set[int] | None = None) -> dict:
    files = total = unique = 0
    for base, _dirs, names in os.walk(root):
        for name in names:
            try:
                st = os.lstat(os.path.join(base, name))
            except OSError:
                continue
            if stat.S_ISLNK(st.st_mode):
                continue
            files += 1
            total += st.st_size
            if exclude_inodes is None or st.st_ino not in exclude_inodes:
                unique += st.st_size
    return {"files": files, "bytes": total, "bytes_not_shared_by_inode": unique}


def inode_set(root: Path) -> set[int]:
    out: set[int] = set()
    for base, _dirs, names in os.walk(root):
        for name in names:
            try:
                st = os.lstat(os.path.join(base, name))
            except OSError:
                continue
            if not stat.S_ISLNK(st.st_mode):
                out.add(st.st_ino)
    return out


def rmtree(path: Path, attempts: int = 20) -> dict:
    """Remove a tree; on Windows clear read-only bits and retry locked files."""
    failures: list[str] = []

    def onerror(func, target, exc):  # noqa: ANN001
        try:
            os.chmod(target, stat.S_IWRITE)
            func(target)
        except OSError as err:
            failures.append(f"{target}: {err}")

    started = time.time()
    for attempt in range(attempts):
        failures.clear()
        if not path.exists() and not path.is_symlink():
            break
        if path.is_symlink() or path.is_file():
            path.unlink()
            break
        shutil.rmtree(path, onexc=onerror) if sys.version_info >= (3, 12) else shutil.rmtree(path, onerror=onerror)
        if not path.exists():
            break
        time.sleep(0.25 * (attempt + 1))
    return {"seconds": round(time.time() - started, 2), "left": path.exists(), "failures": failures[:30], "failure_count": len(failures)}


# --------------------------------------------------------------------------- clone


def _clonefile(src: Path, dst: Path) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    fn = libc.clonefile
    fn.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint32]
    fn.restype = ctypes.c_int
    if fn(os.fsencode(src), os.fsencode(dst), 0) != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err), str(dst))


def clone_tree(src: Path, dst: Path, method: str, skip: set[str] | None = None, jobs: int = 8) -> dict:
    """Clone src -> dst. Methods: hardlink, copy, clonefile (one call for the whole
    tree, macOS), cp-c (`cp -c -R`, per-file clonefile, macOS). `skip` holds
    posix paths relative to src that are left out (hardlink/copy only)."""
    if dst.exists():
        raise StageError("clone", f"destination exists: {dst}")
    started = time.time()
    counts = {"files": 0, "dirs": 0, "symlinks": 0, "skipped": 0}
    if method == "clonefile":
        dst.parent.mkdir(parents=True, exist_ok=True)
        _clonefile(src, dst)
    elif method == "cp-c":
        dst.parent.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(["cp", "-c", "-R", "-p", str(src), str(dst)], capture_output=True, text=True)
        if proc.returncode != 0:
            raise StageError("clone", proc.stderr.strip()[:500])
    elif method in {"hardlink", "copy"}:
        work: list[tuple[Path, Path]] = []
        for base, dirs, names in os.walk(src):
            rel_base = Path(base).relative_to(src)
            target_base = dst / rel_base
            target_base.mkdir(parents=True, exist_ok=True)
            counts["dirs"] += 1
            for name in list(dirs):
                full = Path(base) / name
                if full.is_symlink():
                    os.symlink(os.readlink(full), target_base / name, target_is_directory=True)
                    counts["symlinks"] += 1
                    dirs.remove(name)
            for name in names:
                full = Path(base) / name
                rel = (rel_base / name).as_posix()
                if skip and rel in skip:
                    counts["skipped"] += 1
                    continue
                if full.is_symlink():
                    os.symlink(os.readlink(full), target_base / name)
                    counts["symlinks"] += 1
                    continue
                work.append((full, target_base / name))
        op = os.link if method == "hardlink" else shutil.copy2

        def run(pair: tuple[Path, Path]) -> None:
            op(pair[0], pair[1])

        with ThreadPoolExecutor(max_workers=jobs) as pool:
            list(pool.map(run, work))
        counts["files"] = len(work)
    else:
        raise StageError("clone", f"unknown clone method {method}")
    return {"method": method, "seconds": round(time.time() - started, 2), **counts}


# --------------------------------------------------------------------------- dists, plan


def installed_dists(site_packages: Path) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for info in site_packages.glob("*.dist-info"):
        meta = info / "METADATA"
        if not meta.exists():
            continue
        name = version = None
        with meta.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if line.startswith("Name:") and name is None:
                    name = line.split(":", 1)[1].strip()
                elif line.startswith("Version:") and version is None:
                    version = line.split(":", 1)[1].strip()
                if (name and version) or not line.strip():
                    break
        if name and version:
            out[canon(name)] = {"name": name, "version": version, "dist_info": info.name}
    return out


def dist_files(site_packages: Path, dist_info: str, python_root: Path) -> list[str]:
    """Files a dist owns (from RECORD), as posix paths relative to python_root."""
    record = site_packages / dist_info / "RECORD"
    out = []
    if record.exists():
        for line in record.read_text(encoding="utf-8", errors="replace").splitlines():
            rel = line.rsplit(",", 2)[0] if line.count(",") >= 2 else line.split(",")[0]
            if not rel:
                continue
            full = Path(os.path.normpath(site_packages / rel))
            try:
                out.append(full.relative_to(python_root).as_posix())
            except ValueError:
                continue
    return out


TARGET_PROBE = r"""
import json, os, platform, sys
env = {"implementation_name": sys.implementation.name,
       "implementation_version": "%d.%d.%d" % sys.implementation.version[:3],
       "os_name": os.name, "platform_machine": platform.machine(),
       "platform_release": platform.release(), "platform_system": platform.system(),
       "platform_version": platform.version(), "python_full_version": platform.python_version(),
       "platform_python_implementation": platform.python_implementation(),
       "python_version": ".".join(platform.python_version_tuple()[:2]), "sys_platform": sys.platform}
try:
    from packaging.tags import sys_tags
    tags = [str(t) for t in sys_tags()]
except Exception as exc:
    tags = None
print(json.dumps({"env": env, "tags": tags, "executable": sys.executable, "prefix": sys.prefix}))
"""


def probe_target(python: Path) -> dict:
    proc = subprocess.run([str(python), "-I", "-c", TARGET_PROBE], capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise StageError("plan", f"target probe failed: {proc.stderr.strip()[:400]}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


def load_pylock(path: Path) -> list[dict]:
    return tomllib.loads(path.read_text(encoding="utf-8"))["packages"]


def make_plan(pylock: Path, target: dict, installed: dict[str, dict]) -> dict:
    from packaging.markers import Marker
    from packaging.tags import Tag, parse_tag
    from packaging.utils import parse_wheel_filename
    from packaging.version import Version
    from urllib.parse import unquote

    env = dict(target["env"], extra="")
    if target.get("tags"):
        priority = {Tag(*t.split("-")): i for i, t in enumerate(target["tags"])}
    else:
        from packaging.tags import sys_tags

        priority = {t: i for i, t in enumerate(sys_tags())}
    del parse_tag
    want: dict[str, dict] = {}
    for pkg in load_pylock(pylock):
        marker = pkg.get("marker")
        if marker and not Marker(marker).evaluate(env):
            continue
        want[canon(pkg["name"])] = pkg
    changes, adds, missing_wheels = [], [], []
    for name, pkg in sorted(want.items()):
        inst = installed.get(name)
        if inst is not None and Version(inst["version"]) == Version(pkg["version"]):
            continue
        best = None
        for wheel in pkg.get("wheels") or []:
            fname = unquote(wheel["url"].rsplit("/", 1)[-1])
            rank = min((priority[t] for t in parse_wheel_filename(fname)[3] if t in priority), default=None)
            if rank is not None and (best is None or rank < best[0]):
                best = (rank, wheel, fname)
        entry = {
            "name": pkg["name"],
            "from": inst["version"] if inst else None,
            "to": pkg["version"],
            "index": pkg.get("index"),
        }
        if best is None:
            missing_wheels.append(entry)
            continue
        entry.update({"url": best[1]["url"], "file": best[2], "size": best[1].get("size"), "sha256": best[1]["hashes"]["sha256"]})
        (changes if inst else adds).append(entry)
    removed = [
        {"name": info["name"], "version": info["version"], "dist_info": info["dist_info"]}
        for name, info in sorted(installed.items())
        if name not in want and name not in KEEP_DISTS
    ]
    return {
        "wanted": len(want),
        "installed": len(installed),
        "change": changes,
        "add": adds,
        "remove": removed,
        "no_wheel": missing_wheels,
        "wheel_bytes": sum(e.get("size") or 0 for e in changes + adds),
    }


def write_plan_requirements(plan: dict, path: Path, corrupt: bool = False) -> None:
    lines = []
    for entry in plan["change"] + plan["add"]:
        digest = "0" * 64 if corrupt else entry["sha256"]
        lines.append(f"{entry['name']} @ {entry['url']} --hash=sha256:{digest}")
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def corrupt_export(export: Path, dest: Path, names: list[str]) -> None:
    """Replace every hash of the given packages in a hashed requirements export."""
    wanted = {canon(n) for n in names}
    out, current = [], None
    for line in export.read_text(encoding="utf-8").splitlines():
        head = line.strip()
        if head and not head.startswith(("#", "--")) and not line.startswith(" "):
            current = canon(re.split(r"[=<>!~ ;@]", head, maxsplit=1)[0])
        if current in wanted and "--hash=sha256:" in line:
            line = re.sub(r"sha256:[0-9a-f]{64}", "sha256:" + "0" * 64, line)
        out.append(line)
    dest.write_text("\n".join(out) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------- sync


def run_logged(cmd: list, log: Path, env: dict, timeout: float = 3600, cwd: Path | None = None) -> tuple[int, float]:
    started = time.time()
    with log.open("ab") as handle:
        handle.write(("\n$ " + " ".join(str(c) for c in cmd) + "\n").encode())
        handle.flush()
        try:
            proc = subprocess.run([str(c) for c in cmd], stdout=handle, stderr=subprocess.STDOUT, env=env, timeout=timeout, cwd=cwd)
            code = proc.returncode
        except subprocess.TimeoutExpired:
            code = -999
    return code, round(time.time() - started, 2)


def sync(
    *,
    tool: str,
    source: str,
    python: Path,
    plan: dict,
    work: Path,
    export: Path,
    uv: Path | None,
    proxy: str | None,
    log: Path,
    corrupt: bool = False,
) -> dict:
    """Bring the clone's libraries to the new lock. tool: uv|pip. source:
    export (hashed requirements export + indexes) or plan (direct wheel URLs
    with hashes, no index). Removed packages are uninstalled afterwards."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PYTHON", "UV_", "PIP_", "VIRTUAL_ENV"))}
    env.update({"PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8"})
    if proxy:
        env.update({"HTTPS_PROXY": proxy, "https_proxy": proxy, "HTTP_PROXY": proxy, "http_proxy": proxy, "NO_PROXY": "", "no_proxy": ""})
    result: dict = {"tool": tool, "source": source, "steps": []}
    result["externally_managed_marker"] = [str(p) for p in python.parent.parent.glob("lib/python3.*/EXTERNALLY-MANAGED")][:3] if not IS_WIN else [
        str(p) for p in (python.parent / "Lib").glob("EXTERNALLY-MANAGED")
    ]
    req = work / "requirements-plan.txt"
    if source == "plan":
        write_plan_requirements(plan, req, corrupt=corrupt)
    else:
        if corrupt:
            corrupt_export(export, req, [e["name"] for e in plan["change"] + plan["add"]])
        else:
            shutil.copyfile(export, req)
    to_install = plan["change"] + plan["add"]
    if tool == "uv":
        cache = work / "uv-cache"
        env.update({"UV_NO_CONFIG": "1", "UV_CACHE_DIR": str(cache), "UV_PYTHON_DOWNLOADS": "never", "UV_NO_PROGRESS": "1"})
        base = [uv, "pip", "install", "--python", python, "--break-system-packages", "--require-hashes", "--no-deps", "--no-build"]
        if source == "plan":
            cmd = [*base, "--no-index", "-r", req]
        else:
            cmd = [*base, "--index-url", PYPI, "--extra-index-url", TORCH_INDEX, "--index-strategy", "unsafe-best-match", "-r", req]
        if to_install or source == "export":
            code, seconds = run_logged(cmd, log, env)
            result["steps"].append({"step": "install", "rc": code, "seconds": seconds})
            if code != 0:
                raise StageError("sync", f"uv pip install failed rc={code}")
        if plan["remove"]:
            code, seconds = run_logged([uv, "pip", "uninstall", "--python", python, "--break-system-packages", *[r["name"] for r in plan["remove"]]], log, env)
            result["steps"].append({"step": "uninstall", "rc": code, "seconds": seconds})
            if code != 0:
                raise StageError("sync", f"uv pip uninstall failed rc={code}")
        code, seconds = run_logged([uv, "pip", "check", "--python", python], log, env)
        result["steps"].append({"step": "check", "rc": code, "seconds": seconds})
        result["cache_bytes"] = dir_stats(cache)["bytes"] if cache.exists() else 0
    elif tool == "pip":
        env.update({"PIP_CONFIG_FILE": os.devnull, "PIP_DISABLE_PIP_VERSION_CHECK": "1", "PIP_NO_INPUT": "1"})
        base = [python, "-m", "pip", "install", "--break-system-packages", "--require-hashes", "--only-binary", ":all:", "--no-deps", "--no-compile", "--no-cache-dir", "--no-warn-script-location"]
        if proxy:
            base += ["--proxy", proxy]
        if source == "plan":
            cmd = [*base, "--no-index", "-r", req]
        else:
            cmd = [*base, "--index-url", PYPI, "--extra-index-url", TORCH_INDEX, "-r", req]
        if to_install or source == "export":
            code, seconds = run_logged(cmd, log, env)
            result["steps"].append({"step": "install", "rc": code, "seconds": seconds})
            if code != 0:
                raise StageError("sync", f"pip install failed rc={code}")
        if plan["remove"]:
            code, seconds = run_logged([python, "-m", "pip", "uninstall", "-y", "--break-system-packages", *[r["name"] for r in plan["remove"]]], log, env)
            result["steps"].append({"step": "uninstall", "rc": code, "seconds": seconds})
            if code != 0:
                raise StageError("sync", f"pip uninstall failed rc={code}")
        code, seconds = run_logged([python, "-m", "pip", "check"], log, env)
        result["steps"].append({"step": "check", "rc": code, "seconds": seconds})
    else:
        raise StageError("sync", f"unknown tool {tool}")
    result["seconds"] = round(sum(s["seconds"] for s in result["steps"]), 2)
    return result


# --------------------------------------------------------------------------- parts


def fetch(url: str, dest: Path, sha256: str, size: int | None) -> dict:
    """Download (http(s) or file path) to dest, verify SHA-256 before anything uses it."""
    started = time.time()
    digest = hashlib.sha256()
    total = 0
    tmp = dest.with_name(dest.name + ".part")
    if url.startswith(("http://", "https://")):
        host = url.split("/")[2].split(":")[0]
        if host not in ALLOWED_HOSTS and not host.endswith(".githubusercontent.com") and host != "github.com":
            raise StageError("download", f"host not allowed: {host}")
        source = urllib.request.urlopen(url, timeout=60)  # noqa: S310 - allow-listed hosts
    else:
        source = open(url, "rb")  # noqa: SIM115
    with source, tmp.open("wb") as out:
        for chunk in iter(lambda: source.read(1 << 20), b""):
            digest.update(chunk)
            total += len(chunk)
            out.write(chunk)
    if digest.hexdigest() != sha256 or (size is not None and total != size):
        tmp.unlink(missing_ok=True)
        raise StageError("download", f"{dest.name}: sha256/size mismatch (got {digest.hexdigest()[:16]}..., {total} bytes)")
    os.replace(tmp, dest)
    return {"file": dest.name, "bytes": total, "seconds": round(time.time() - started, 2)}


def unpack(archive: Path, dest: Path) -> dict:
    started = time.time()
    count = 0
    with tarfile.open(archive, "r:*") as tar:
        members = tar.getmembers()
        count = len(members)
        tar.extractall(dest, filter="data")
    return {"archive": archive.name, "members": count, "seconds": round(time.time() - started, 2)}


def compile_bytecode(python: Path, staging: Path, targets: list[Path], list_file: Path, log: Path, env: dict) -> dict:
    """Deterministic, path-independent bytecode for the new code and changed libraries."""
    files = [str(t) for t in targets if t.is_file() and t.suffix == ".py"]
    dirs = [str(t) for t in targets if t.is_dir()]
    list_file.write_text("\n".join(files) + "\n", encoding="utf-8")
    env = dict(env)
    env.pop("PYTHONDONTWRITEBYTECODE", None)
    cmd = [python, "-m", "compileall", "-q", "-j", "0", "-s", str(staging), "--invalidation-mode", "unchecked-hash", "-i", list_file, *dirs]
    code, seconds = run_logged(cmd, log, env)
    dir_py = sum(1 for d in dirs for _ in Path(d).rglob("*.py"))
    return {"rc": code, "seconds": seconds, "py_files": len(files) + dir_py, "library_py_files": len(files)}


SERVER_IMPORT = (
    "import sys, time, os; app = os.getcwd(); sys.path[:0] = [os.path.join(app, 'src'), app]; t = time.time();"
    " import row_bot.server, row_bot.version; v = row_bot.version.__version__;"
    " print('version', v, 'seconds', round(time.time() - t, 2)); sys.exit(0 if v == sys.argv[1] else 3)"
)
APP_IMPORT = (
    "import sys, time, os; app = os.getcwd(); sys.path[:0] = [os.path.join(app, 'src'), app]; t = time.time();"
    " import row_bot.app; print('row_bot.app imported', round(time.time() - t, 2)); os._exit(0)"
)


def self_test(vroot: Path, version: str, log: Path, work: Path) -> dict:
    """verify_runtime_dependencies.py all + import row_bot.server (and row_bot.app)
    with the staged Python, a scratch data folder and a scrubbed environment."""
    data = work / "selftest-data"
    data.mkdir(parents=True, exist_ok=True)
    env = runtime_env(vroot, data)
    python = Layout.python_exe(vroot)
    app = vroot / "app"
    out: dict = {}
    for label, cmd, timeout in (
        ("verify_runtime_dependencies", [python, app / "scripts" / "verify_runtime_dependencies.py", "all"], 900),
        ("import_row_bot_server", [python, "-c", SERVER_IMPORT, version], 300),
        ("import_row_bot_app", [python, "-c", APP_IMPORT], 300),
    ):
        code, seconds = run_logged(cmd, log, env, timeout=timeout, cwd=app)
        out[label] = {"rc": code, "seconds": seconds}
    out["ok"] = all(v["rc"] == 0 for v in out.values() if isinstance(v, dict))
    return out


# --------------------------------------------------------------------------- stage


def stage(
    *,
    layout: Layout,
    current: str,
    new: str,
    expect_version: str,
    manifest: dict,
    parts_base: str,
    pylock: Path,
    export: Path,
    clone_method: str,
    tool: str,
    source: str,
    uv: Path | None,
    proxy: str | None,
    log: Path,
    prune: bool = False,
    inject: str | None = None,
    keep_on_failure: bool = False,
    selftest: bool = True,
    force_assets: bool = False,
    stop_after: str | None = None,
    linux_shell_from: Path | None = None,
) -> dict:
    """Stage `new` next to `current`. Returns a report; raises nothing (errors
    are reported, the staging folder is removed and state.json is untouched)."""
    timings: dict[str, float] = {}
    report: dict = {"current": current, "new": new, "clone_method": clone_method, "tool": tool, "source": source, "prune": prune, "inject": inject, "timings": timings}
    src_root = layout.version_root(current)
    staging = layout.versions / f"{new}.staging"
    final = layout.versions / new
    work = layout.root / ".work" / f"{new}-{int(time.time())}"
    state_before = read_state(layout.root)
    free_before = shutil.disk_usage(layout.root).free
    t_all = time.time()
    try:
        for leftover in (staging, final):
            if leftover.exists():
                rmtree(leftover)
        work.mkdir(parents=True, exist_ok=True)
        staging.mkdir(parents=True)

        # 1. download + verify parts (code always; assets only if their hash differs).
        t = time.time()
        downloads = []
        current_assets = content_hash(src_root / "app", ASSET_DIRS)
        report["assets_content_current"] = current_assets
        for part in ("code", "assets"):
            info = manifest[part]
            if part == "assets" and not force_assets and info.get("content_sha256") == current_assets:
                continue
            url = parts_base.rstrip("/") + "/" + info["file"]
            downloads.append(fetch(url, work / info["file"], info["sha256"], info.get("size")))
        report["downloads"] = downloads
        timings["download_parts"] = round(time.time() - t, 2)

        # 2. clone python/
        src_python = src_root / "python"
        src_sp = Layout.site_packages(src_root)
        installed = installed_dists(src_sp)
        target = probe_target(Layout.python_exe(src_root))
        report["target"] = {"env": target["env"], "tags": len(target.get("tags") or [])}
        t = time.time()
        plan = make_plan(pylock, target, installed)
        timings["plan"] = round(time.time() - t, 2)
        report["plan"] = plan
        if plan["no_wheel"]:
            raise StageError("plan", "no wheel for: " + ", ".join(f"{e['name']}=={e['to']}" for e in plan["no_wheel"]))
        skip: set[str] | None = None
        if prune:
            skip = set()
            for entry in plan["change"] + plan["remove"]:
                info = installed[canon(entry["name"])]
                skip.update(dist_files(src_sp, info["dist_info"], src_python))
            report["pruned_files"] = len(skip)
        clone = clone_tree(src_python, staging / "python", clone_method, skip=skip)
        if prune and clone_method in {"clonefile", "cp-c"}:
            for rel in sorted(skip or ()):
                (staging / "python" / rel).unlink(missing_ok=True)
        if prune:
            # Directories the pruned dists owned (e.g. their .dist-info) are now empty.
            parents = sorted({(staging / "python" / rel).parent for rel in skip or ()}, key=lambda p: len(p.parts), reverse=True)
            for folder in parents:
                while folder != staging / "python" and folder.is_dir() and not any(folder.iterdir()):
                    folder.rmdir()
                    folder = folder.parent
        report["clone"] = clone
        timings["clone"] = clone["seconds"]

        # 3. sync libraries
        stage_python = Layout.python_exe(staging)
        t = time.time()
        report["sync"] = sync(
            tool=tool,
            source=source,
            python=stage_python,
            plan=plan,
            work=work,
            export=export,
            uv=uv,
            proxy=proxy,
            log=log,
            corrupt=inject == "sync-bad-hash",
        )
        timings["sync"] = round(time.time() - t, 2)
        after = installed_dists(Layout.site_packages(staging))
        mismatched = [
            e["name"] for e in plan["change"] + plan["add"] if after.get(canon(e["name"]), {}).get("version") != e["to"]
        ]
        still_there = [r["name"] for r in plan["remove"] if canon(r["name"]) in after]
        report["sync_result"] = {"version_mismatch": mismatched, "not_removed": still_there}
        if mismatched or still_there:
            raise StageError("sync", f"clone does not match the lock: {mismatched} {still_there}")
        if stop_after == "sync":
            report["ok"] = True
            report["stopped_after"] = "sync"
            report["cleanup"] = rmtree(staging)
            return report

        # 4. unpack code + assets
        t = time.time()
        app = staging / "app"
        app.mkdir()
        report["unpack"] = [unpack(work / d["file"], app) for d in downloads]
        if len(downloads) == 1:  # assets unchanged: reuse the current version's files
            method = clone_method if clone_method in {"clonefile", "cp-c", "hardlink"} else "copy"
            report["assets_reused"] = [clone_tree(src_root / "app" / d, app / d, method) for d in ASSET_DIRS]
        if linux_shell_from is not None:
            for name in ("bin", "share", "install.sh", "uninstall.sh"):
                src = linux_shell_from / name
                if src.is_dir():
                    shutil.copytree(src, staging / name, symlinks=True)
                elif src.exists():
                    shutil.copy2(src, staging / name)
            info_path = linux_shell_from / "install_info.json"
            if info_path.exists():
                info_data = json.loads(info_path.read_text(encoding="utf-8"))
                info_data["version"] = expect_version
                (staging / "install_info.json").write_text(json.dumps(info_data, indent=2), encoding="utf-8")
        timings["unpack"] = round(time.time() - t, 2)
        if inject == "break-app":
            (app / "src" / "row_bot" / "server.py").unlink()
        if inject == "break-lib":
            for entry in plan["change"]:
                pkg_dir = Layout.site_packages(staging) / entry["name"].replace("-", "_").lower()
                if pkg_dir.is_dir():
                    rmtree(pkg_dir)
                    report["broken_library"] = pkg_dir.name
                    break

        # 5. compile bytecode for what changed
        changed_targets = [app]
        new_sp = Layout.site_packages(staging)
        for entry in plan["change"] + plan["add"]:
            info = after.get(canon(entry["name"]))
            if info:
                changed_targets.extend(staging / "python" / rel for rel in dist_files(new_sp, info["dist_info"], staging / "python") if rel.endswith(".py"))
        report["compile"] = compile_bytecode(stage_python, staging, changed_targets, work / "compile-list.txt", log, runtime_env(staging))
        timings["compile"] = report["compile"]["seconds"]

        # 6. self-test
        if selftest:
            t = time.time()
            report["selftest"] = self_test(staging, expect_version, log, work)
            timings["selftest"] = round(time.time() - t, 2)
            if not report["selftest"]["ok"]:
                raise StageError("selftest", json.dumps({k: v for k, v in report["selftest"].items() if k != "ok"}))

        # 7. finish: version.json, rename, state.ready
        t = time.time()
        (staging / "version.json").write_text(
            json.dumps(
                {
                    "version": expect_version,
                    "label": new,
                    "python": target["env"]["python_full_version"],
                    "lock_sha256": manifest.get("lock_sha256"),
                    "parts": {p: manifest[p]["sha256"] for p in ("code", "assets")},
                    "staged_at": now_iso(),
                    "state": "ready",
                    "clone_method": clone_method,
                    "sync": f"{tool}/{source}",
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        ready_name = new
        for attempt in range(40):
            try:
                os.replace(staging, final)
                report["rename_attempts"] = attempt + 1
                break
            except PermissionError as exc:
                if attempt == 39:
                    # Keep going in place so the switch can still be measured; the report says so.
                    report["rename_error"] = repr(exc)
                    ready_name = staging.name
                time.sleep(0.25)
        report["ready_version"] = ready_name
        state = read_state(layout.root) or new_state(current)
        state["ready"] = {"version": ready_name, "staged_at": now_iso()}
        write_state(layout.root, state)
        timings["finish"] = round(time.time() - t, 2)
        report["ok"] = True
    except Exception as exc:  # noqa: BLE001 - a spike reports every failure
        report["ok"] = False
        report["error"] = {"step": getattr(exc, "step", type(exc).__name__), "message": str(exc)[:2000]}
        if not keep_on_failure:
            report["cleanup"] = rmtree(staging)
        report["state_unchanged"] = read_state(layout.root) == state_before
    finally:
        timings["total"] = round(time.time() - t_all, 2)
        free_after = shutil.disk_usage(layout.root).free
        report["disk_free_delta_bytes"] = free_before - free_after
        rmtree(work)
        report["staging_left"] = staging.exists()
    return report
