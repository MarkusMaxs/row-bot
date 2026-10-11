"""Velopack trial driver (Phase 0 spike, CI only).

Subcommands:
  sizes      full/delta sizes, delta composition, nupkg-level diff by category
  scenarios  install 5.0 and update to 5.1 under several conditions, timing
             everything; Windows uses Setup.exe, macOS the portable .app zip

Everything is written as JSON under --out. Nothing here talks to anything but
the local file feed and a loopback HTTP server.
"""

from __future__ import annotations

import argparse
import functools
import hashlib
import http.server
import json
import os
import re
import shutil
import socketserver
import subprocess
import sys
import threading
import time
import zipfile
import zlib
from pathlib import Path

IS_WIN = os.name == "nt"
PACK_ID = "RowBotTrial"
CHANNEL = "win" if IS_WIN else "osx"
HOME = Path.home() / "rowbot-velopack-trial"
TRIAL_LOG = HOME / "trial.jsonl"
CONTROL = HOME / "control.json"

if IS_WIN:
    ROOT = Path(os.environ["LOCALAPPDATA"]) / PACK_ID
    INSTALL = ROOT / "current"
    PACKAGES = ROOT / "packages"
    VLOG = Path(os.environ["LOCALAPPDATA"]) / "velopack" / f"velopack_{PACK_ID}.log"
else:
    ROOT = Path("/Applications/Row-Bot.app")
    INSTALL = ROOT
    PACKAGES = Path.home() / "Library" / "Caches" / "velopack" / PACK_ID / "packages"
    VLOG = Path.home() / "Library" / "Logs" / f"velopack_{PACK_ID}.log"


def sh(cmd: list, timeout: float | None = None, **kw) -> subprocess.CompletedProcess:
    return subprocess.run([str(c) for c in cmd], capture_output=True, text=True, timeout=timeout, **kw)  # noqa: S603


# ----------------------------------------------------------------------------- helpers


def package(feed: Path, version: str, kind: str) -> Path:
    """vpk names packages <id>-<version>-full.nupkg on Windows, <id>-<version>-osx-full.nupkg on macOS."""
    matches = sorted(feed.glob(f"{PACK_ID}-{version}*-{kind}.nupkg"))
    if len(matches) != 1:
        raise FileNotFoundError(f"{kind} package for {version} in {feed}: {[m.name for m in matches]}")
    return matches[0]


def category(path: str) -> str:
    p = path.lower()
    if p.endswith(".pyc"):
        return "pyc"
    if p.endswith((".so", ".dylib", ".pyd", ".dll", ".exe")) or "/contents/macos/" in f"/{p}" or "/bin/" in p:
        return "native"
    if "_codesignature/" in p:
        return "bundle-signature"
    if "static/client-v2/" in p:
        return "client"
    if "site-packages/" not in p and ("row_bot/" in p or "/app/" in f"/{p}"):
        return "row-bot-app"
    if ".dist-info/" in p:
        return "dist-info"
    if p.endswith(".py"):
        return "py"
    return "other"


def bump(table: dict, key: str, count: int = 1, size: int = 0) -> None:
    row = table.setdefault(key, {"count": 0, "bytes": 0})
    row["count"] += count
    row["bytes"] += size


def dir_size(path: Path) -> dict:
    total = files = 0
    if path.exists():
        for base, _dirs, names in os.walk(path):
            for name in names:
                try:
                    st = os.lstat(os.path.join(base, name))
                except OSError:
                    continue
                total += st.st_size
                files += 1
    return {"bytes": total, "files": files}


def pid_alive(pid: int) -> bool:
    if IS_WIN:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, int(pid))
        if not handle:
            return False
        code = ctypes.c_ulong()
        kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        kernel32.CloseHandle(handle)
        return code.value == 259
    try:
        os.kill(int(pid), 0)
    except OSError:
        return False
    return True


def processes() -> list[dict]:
    if IS_WIN:
        script = (
            "Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -like '*RowBotTrial*' -or "
            "$_.CommandLine -like '*RowBotTrial*' -or $_.CommandLine -like '*rowbot-velopack-trial*' } | "
            "Select-Object ProcessId,ParentProcessId,ExecutablePath,CommandLine | ConvertTo-Json -Depth 2"
        )
        out = sh(["pwsh", "-NoProfile", "-Command", script]).stdout.strip()
        if not out:
            return []
        data = json.loads(out)
        return data if isinstance(data, list) else [data]
    out = sh(["ps", "-axo", "pid=,ppid=,command="]).stdout
    rows = []
    for line in out.splitlines():
        if "Row-Bot.app" in line or "UpdateMac" in line or PACK_ID in line:
            parts = line.split(None, 2)
            if len(parts) == 3:
                rows.append({"pid": int(parts[0]), "ppid": int(parts[1]), "command": parts[2][:300]})
    return rows


def kill_everything(extra_pids: list[int] | None = None) -> None:
    for pid in extra_pids or []:
        if pid_alive(pid):
            if IS_WIN:
                sh(["taskkill", "/F", "/T", "/PID", pid])
            else:
                sh(["kill", "-9", pid])
    if IS_WIN:
        sh(
            [
                "pwsh",
                "-NoProfile",
                "-Command",
                "Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -like '*RowBotTrial*' } | "
                "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }",
            ]
        )
    else:
        sh(["pkill", "-9", "-f", "Row-Bot.app"])
        sh(["pkill", "-9", "-f", "UpdateMac"])
    time.sleep(1.5)


def events() -> list[dict]:
    if not TRIAL_LOG.exists():
        return []
    rows = []
    for line in TRIAL_LOG.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            pass
    return rows


def wait_event(pred, timeout: float, since: int = 0) -> dict | None:
    end = time.time() + timeout
    while time.time() < end:
        for event in events()[since:]:
            if pred(event):
                return event
        time.sleep(0.2)
    return None


def write_control(data: dict) -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    CONTROL.write_text(json.dumps(data), encoding="utf-8")


def reset_trial_home() -> None:
    if HOME.exists():
        shutil.rmtree(HOME, ignore_errors=True)
    HOME.mkdir(parents=True, exist_ok=True)


def vlog_offset() -> int:
    return VLOG.stat().st_size if VLOG.exists() else 0


def vlog_since(offset: int) -> str:
    if not VLOG.exists():
        return ""
    with VLOG.open("rb") as handle:
        handle.seek(offset)
        return handle.read().decode("utf-8", errors="replace")


class DiskSampler:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.start_free = shutil.disk_usage(path).free
        self.min_free = self.start_free
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self._stop.is_set():
            self.min_free = min(self.min_free, shutil.disk_usage(self.path).free)
            time.sleep(0.25)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join()

    def result(self) -> dict:
        return {"peak_extra_bytes": self.start_free - self.min_free}


class NetSampler:
    """macOS: sample lsof for non-loopback sockets of trial processes (weak evidence)."""

    def __init__(self) -> None:
        self.seen: dict[str, list] = {}
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self._stop.is_set():
            out = sh(["lsof", "-nP", "-i"], timeout=20).stdout if not IS_WIN else ""
            for line in out.splitlines()[1:]:
                if not any(k in line for k in ("UpdateMac", "python", "row-bot", "Row-Bot")):
                    continue
                if "127.0.0.1" in line or "[::1]" in line or "localhost" in line:
                    continue
                key = " ".join(line.split()[:2]) + " " + line.split()[-2] if len(line.split()) > 2 else line
                self.seen.setdefault(key, []).append(time.time())
            time.sleep(1)

    def __enter__(self):
        if not IS_WIN:
            self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join()


# ----------------------------------------------------------------------------- sizes


def delta_composition(path: Path) -> dict:
    size = path.stat().st_size
    with zipfile.ZipFile(path) as archive:
        infos = [i for i in archive.infolist() if not i.is_dir()]
    kinds: dict = {}
    cats: dict = {}
    compressed = 0
    for info in infos:
        name = info.filename
        compressed += info.compress_size
        if not name.startswith("lib/"):
            bump(kinds, "metadata", 1, info.compress_size)
            continue
        original = name[len("lib/app/") :] if name.startswith("lib/app/") else name[4:]
        kind = "new-file"
        for suffix in (".zsdiff", ".bsdiff", ".shasum", ".diff"):
            if original.endswith(suffix):
                original = original[: -len(suffix)]
                kind = suffix[1:]
                break
        if kind == "diff" and info.file_size == 0:
            kind = "unchanged-marker"
        elif kind == "shasum" and info.file_size == 0:
            kind = "unchanged-marker"
        bump(kinds, kind, 1, info.compress_size)
        if kind != "unchanged-marker":
            bump(cats, category(original), 1, info.compress_size)
    return {
        "file": path.name,
        "bytes": size,
        "entries": len(infos),
        "compressed_payload_bytes": compressed,
        "zip_overhead_bytes": size - compressed,
        "by_kind": kinds,
        "changed_or_new_by_category": cats,
    }


def lib_prefix(path: Path) -> str:
    """Archive prefix that maps to INSTALL (lib/app/ or lib/app/<Name>.app/)."""
    with zipfile.ZipFile(path) as archive:
        names = [i.filename for i in archive.infolist() if i.filename.startswith("lib/app/")]
    if IS_WIN or any(n.startswith("lib/app/Contents/") for n in names):
        return "lib/app/"
    for n in names:
        m = re.match(r"(lib/app/[^/]+\.app/)Contents/", n)
        if m:
            return m.group(1)
    return "lib/app/"


def nupkg_index(path: Path) -> dict[str, tuple[int, int]]:
    with zipfile.ZipFile(path) as archive:
        return {
            i.filename[len("lib/app/") :]: (i.CRC, i.file_size)
            for i in archive.infolist()
            if i.filename.startswith("lib/app/") and not i.is_dir()
        }


def nupkg_diff(old: Path, new: Path) -> dict:
    a, b = nupkg_index(old), nupkg_index(new)
    table: dict = {}
    for name, (crc, size) in b.items():
        if name not in a:
            bump(table, f"new/{category(name)}", 1, size)
        elif a[name] != (crc, size):
            bump(table, f"changed/{category(name)}", 1, size)
        else:
            bump(table, "same", 1, size)
    for name, (_crc, size) in a.items():
        if name not in b:
            bump(table, f"removed/{category(name)}", 1, size)
    return {"old_files": len(a), "new_files": len(b), "table": dict(sorted(table.items()))}


def cmd_sizes(args: argparse.Namespace) -> None:
    feed = args.feed
    full_old = package(feed, args.base, "full")
    full_new = package(feed, args.target, "full")
    delta = package(feed, args.target, "delta")
    result: dict = {
        "os": "windows" if IS_WIN else "macos",
        "variant": args.variant,
        "full": {v: p.stat().st_size for v, p in ((args.base, full_old), (args.target, full_new))},
        "installer": {p.name: p.stat().st_size for p in args.installers},
        "delta": delta_composition(delta),
        "nupkg_diff": nupkg_diff(full_old, full_new),
        "extra_deltas": {},
    }
    for spec in args.extra_delta or []:
        label, _, path = spec.partition("=")
        result["extra_deltas"][label] = delta_composition(Path(path))
    for spec in args.extra_diff or []:
        label, _, pair = spec.partition("=")
        old, _, new = pair.partition(",")
        result.setdefault("extra_diffs", {})[label] = nupkg_diff(Path(old), Path(new))
    for path in args.payload_reports or []:
        result.setdefault("payloads", []).append(json.loads(Path(path).read_text(encoding="utf-8")))
    args.out.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps({k: result[k] for k in ("full", "installer")}, indent=2))
    print("delta bytes:", result["delta"]["bytes"], "overhead:", result["delta"]["zip_overhead_bytes"])


# ----------------------------------------------------------------------------- feeds


def releases_files(feed: Path) -> list[Path]:
    return [p for p in feed.iterdir() if p.name.startswith(("releases.", "RELEASES", "assets."))]


def copy_feed(src: Path, dst: Path, target: str) -> Path:
    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True)
    for p in releases_files(src):
        shutil.copy2(p, dst / p.name)
    for path in (package(src, target, "full"), package(src, target, "delta")):
        shutil.copy2(path, dst / path.name)
    return dst


def rehash(feed: Path, name: str) -> None:
    data = (feed / name).read_bytes()
    sha1, sha256, size = hashlib.sha1(data).hexdigest().upper(), hashlib.sha256(data).hexdigest().upper(), len(data)  # noqa: S324
    for p in releases_files(feed):
        text = p.read_text(encoding="utf-8-sig")
        if p.suffix == ".json":
            doc = json.loads(text)
            for asset in doc.get("Assets", []):
                if asset.get("FileName") == name:
                    asset["SHA1"], asset["SHA256"], asset["Size"] = sha1, sha256, size
            p.write_text(json.dumps(doc), encoding="utf-8")
        else:
            lines = []
            for line in text.splitlines():
                parts = line.split()
                if len(parts) >= 3 and parts[1] == name:
                    parts[0], parts[2] = sha1, str(size)
                    line = " ".join(parts)
                lines.append(line)
            p.write_text("\n".join(lines) + "\n", encoding="utf-8")


def rewrite_delta(path: Path, choose) -> str:
    tmp = path.with_suffix(".tmp")
    with zipfile.ZipFile(path) as src:
        infos = src.infolist()
        target = choose(infos)
        with zipfile.ZipFile(tmp, "w") as dst:
            for info in infos:
                data = src.read(info)
                if info.filename == target.filename:
                    mid = len(data) // 2
                    span = min(64, max(1, len(data) // 4))
                    data = data[:mid] + bytes(b ^ 0x5A for b in data[mid : mid + span]) + data[mid + span :]
                dst.writestr(info, data, compress_type=info.compress_type)
    os.replace(tmp, path)
    return target.filename


def make_bad_feed(src: Path, dst: Path, target: str, mode: str) -> dict:
    copy_feed(src, dst, target)
    delta = package(dst, target, "delta")
    detail: dict = {"mode": mode}
    if mode == "delta_bytes":
        data = bytearray(delta.read_bytes())
        mid = len(data) // 2
        for i in range(mid, min(len(data), mid + 4096)):
            data[i] ^= 0xFF
        delta.write_bytes(bytes(data))
    elif mode == "delta_zsdiff_rehash":
        detail["entry"] = rewrite_delta(
            delta, lambda infos: max((i for i in infos if i.filename.endswith(".zsdiff")), key=lambda i: i.file_size)
        )
        rehash(dst, delta.name)
    elif mode == "delta_newfile_rehash":

        def pick(infos):
            plain = [
                i
                for i in infos
                if i.filename.startswith("lib/app/")
                and not i.filename.endswith((".zsdiff", ".bsdiff", ".shasum", ".diff"))
                and i.filename.endswith((".py", ".js", ".json", ".txt", ".md"))
                and i.file_size > 512
            ]
            return max(plain, key=lambda i: i.file_size)

        detail["entry"] = rewrite_delta(delta, pick)
        rehash(dst, delta.name)
    return detail


def synthesize_base(template: Path, out: Path) -> dict:
    """Build a base full package from the *installed* files plus the template's metadata."""
    started = time.time()
    with zipfile.ZipFile(template) as src:
        infos = src.infolist()
        prefix = lib_prefix(template)
        lib = [i for i in infos if i.filename.startswith("lib/app/")]
        meta = [i for i in infos if not i.filename.startswith("lib/")]
        attrs = {i.filename: i for i in lib}
        tmp = out.with_suffix(".tmp")
        written = 0
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as dst:
            for info in meta:
                dst.writestr(info, src.read(info), compress_type=info.compress_type)
            for base, dirs, names in os.walk(INSTALL):
                dirs.sort()
                for name in sorted(names) + [d for d in dirs if os.path.islink(os.path.join(base, d))]:
                    full = Path(base) / name
                    rel = full.relative_to(INSTALL).as_posix()
                    arc = f"{prefix}{rel}"
                    zinfo = zipfile.ZipInfo(arc, date_time=(2020, 1, 1, 0, 0, 0))
                    template_info = attrs.get(arc)
                    if template_info is not None:
                        zinfo.external_attr = template_info.external_attr
                        zinfo.create_system = template_info.create_system
                    if full.is_symlink():
                        if template_info is None:
                            zinfo.external_attr = (0o120777 << 16)
                            zinfo.create_system = 3
                        dst.writestr(zinfo, os.readlink(full), compress_type=zipfile.ZIP_STORED)
                    else:
                        if template_info is None:
                            zinfo.external_attr = (0o100755 << 16)
                            zinfo.create_system = 3
                        dst.writestr(zinfo, full.read_bytes(), compress_type=zipfile.ZIP_DEFLATED)
                    written += 1
    os.replace(tmp, out)
    return {"entries": written, "seconds": round(time.time() - started, 1), "bytes": out.stat().st_size}


# ----------------------------------------------------------------------------- install


def install_sizes() -> dict:
    sizes = {"install": dir_size(INSTALL), "packages": dir_size(PACKAGES)}
    if IS_WIN:
        sizes["root_total"] = dir_size(ROOT)
    sizes["packages_listing"] = sorted(p.name for p in PACKAGES.iterdir()) if PACKAGES.exists() else []
    return sizes


def uninstall() -> dict:
    kill_everything()
    result: dict = {}
    if IS_WIN:
        update = ROOT / "Update.exe"
        if update.exists():
            started = time.time()
            proc = sh([update, "uninstall", "--silent"], timeout=300)
            result = {"rc": proc.returncode, "seconds": round(time.time() - started, 1)}
        result["leftover_root"] = dir_size(ROOT) if ROOT.exists() else None
        shutil.rmtree(ROOT, ignore_errors=True)
    else:
        shutil.rmtree(ROOT, ignore_errors=True)
        shutil.rmtree(PACKAGES.parent, ignore_errors=True)
    return result


def install(args: argparse.Namespace, logdir: Path) -> dict:
    uninstall()
    reset_trial_home()
    write_control({"action": "idle", "idle_seconds": 1})
    started = time.time()
    if IS_WIN:
        proc = sh([args.setup, "--silent", "--log", logdir / "setup.log"], timeout=900)
        installer_seconds = time.time() - started
        # Setup --silent runs the install hook but does not start the app.
        hook = wait_event(lambda e: e["event"] == "hook_after_install", 30)
        result = {
            "rc": proc.returncode,
            "setup_seconds": round(installer_seconds, 1),
            "install_hook_seconds_after_setup_start": round(hook["t"] - started, 1) if hook else None,
        }
    else:
        stage = HOME / "unzip"
        proc = sh(["ditto", "-x", "-k", args.portable, stage], timeout=900)
        apps = list(stage.glob("*.app")) or list(stage.glob("*/*.app"))
        shutil.move(str(apps[0]), str(ROOT))
        shutil.rmtree(stage, ignore_errors=True)
        installer_seconds = time.time() - started
        launched = time.time()
        sh(["open", ROOT])
        first = wait_event(lambda e: e["event"] == "after_run", 120)
        result = {
            "rc": proc.returncode,
            "unzip_seconds": round(installer_seconds, 1),
            "open_to_python_after_run_seconds": round(first["t"] - launched, 1) if first else None,
        }
        time.sleep(2)
        kill_everything()
    result["hooks"] = [e["event"] for e in events() if e["event"].startswith("hook_")]
    result["sizes"] = install_sizes()
    return result


def code_signature() -> dict:
    if IS_WIN:
        return {}
    verify = sh(["codesign", "--verify", "--deep", "--strict", "--verbose=2", ROOT])
    spctl = sh(["spctl", "--assess", "--type", "execute", "-vv", ROOT])
    detail = sh(["codesign", "-dv", "--verbose=4", ROOT])
    team = re.search(r"TeamIdentifier=(\S+)", detail.stderr)
    cdhash = re.search(r"CDHash=(\S+)", detail.stderr)
    return {
        "codesign_verify_rc": verify.returncode,
        "codesign_verify": (verify.stdout + verify.stderr)[-1500:],
        "spctl_rc": spctl.returncode,
        "spctl": (spctl.stdout + spctl.stderr)[-800:],
        "team_id": team.group(1) if team else None,
        "cdhash": cdhash.group(1) if cdhash else None,
    }


def verify_tree(nupkg: Path) -> dict:
    prefix = lib_prefix(nupkg)
    with zipfile.ZipFile(nupkg) as archive:
        entries = [i for i in archive.infolist() if i.filename.startswith(prefix) and not i.is_dir()]
    expected = set()
    missing, mismatched = [], []
    for info in entries:
        rel = info.filename[len(prefix) :]
        expected.add(rel)
        path = INSTALL / rel
        is_link = (info.external_attr >> 16) & 0o170000 == 0o120000
        if is_link:
            if not path.is_symlink():
                mismatched.append(rel + " (symlink expected)")
            continue
        if not path.exists():
            missing.append(rel)
            continue
        crc = 0
        size = 0
        with path.open("rb") as handle:
            for chunk in iter(functools.partial(handle.read, 1 << 20), b""):
                crc = zlib.crc32(chunk, crc)
                size += len(chunk)
        if crc != info.CRC or size != info.file_size:
            mismatched.append(rel)
    extra = []
    for base, _dirs, names in os.walk(INSTALL):
        for name in names:
            rel = (Path(base) / name).relative_to(INSTALL).as_posix()
            if rel not in expected:
                extra.append(rel)
    return {
        "expected": len(entries),
        "missing": len(missing),
        "mismatched": len(mismatched),
        "extra": len(extra),
        "missing_sample": missing[:10],
        "mismatched_sample": mismatched[:10],
        "extra_sample": extra[:20],
    }


def launch_app() -> None:
    if IS_WIN:
        subprocess.Popen([str(INSTALL / "Row-Bot.exe")], cwd=str(INSTALL))  # noqa: S603
    else:
        sh(["open", ROOT])


def update(feed: str, opts: dict, timeout_restart: float = 300) -> dict:
    since = len(events())
    offset = vlog_offset()
    control = {
        "action": "update",
        "feed": feed,
        "children": opts.get("children", False),
        "quit_host": opts.get("quit_host", False),
        "apply": opts.get("apply", True),
        "next": {"action": "idle", "start_server_after_restart": opts.get("server_after_restart", False)},
    }
    write_control(control)
    result: dict = {"control": control}
    with DiskSampler(Path.home()) as disk:
        launched = time.time()
        launch_app()
        done = wait_event(
            lambda e: e["event"] in ("downloaded", "download_failed", "no_update", "crashed", "import_failed"),
            900,
            since,
        )
        result["download_outcome"] = done
        checked = wait_event(lambda e: e["event"] == "checked", 1, since)
        result["checked"] = checked
        if done and done["event"] == "downloaded" and control["apply"]:
            exiting = wait_event(lambda e: e["event"] == "exiting", 600, since)
            target = opts.get("target_version")
            restarted = (
                wait_event(
                    lambda e: e["event"] == "start"
                    and e["t"] > exiting["t"]
                    and (target is None or e.get("installed_version") == target),
                    timeout_restart,
                    since,
                )
                if exiting
                else None
            )
            first_after = wait_event(lambda e: e["event"] == "start" and exiting and e["t"] > exiting["t"], 1, since)
            result["exiting_t"] = exiting["t"] if exiting else None
            result["restarted"] = restarted
            result["first_start_after_exit"] = first_after
            if exiting and restarted:
                result["apply_relaunch_seconds"] = round(restarted["t"] - exiting["t"], 2)
            if opts.get("server_after_restart") and restarted:
                server = wait_event(
                    lambda e: e["event"] == "server_child" and e["t"] > restarted["t"], 300, since
                )
                result["server_after_restart"] = server
            time.sleep(3)
    result["launch_t"] = launched
    result["disk"] = disk.result()
    evs = events()[since:]
    result["events"] = [
        {k: v for k, v in e.items() if k not in ("python",)} for e in evs if e["event"] != "control"
    ]
    starts = [e for e in evs if e["event"] == "start"]
    result["starts"] = [
        {"t": round(e["t"] - launched, 2), "pid": e["pid"], "restart": e.get("env_restart"), "installed": e.get("installed_version")}
        for e in starts
    ]
    child_pids = []
    for e in evs:
        if e["event"] == "children":
            child_pids = [c["pid"] for c in e.get("children", [])]
    result["children_alive_after"] = {str(pid): pid_alive(pid) for pid in child_pids}
    result["processes_after"] = processes()
    result["velopack_log"] = vlog_since(offset)[-20000:]
    result["sizes_after"] = install_sizes()
    return result


class _Handler(http.server.SimpleHTTPRequestHandler):
    requests: list = []

    def log_message(self, fmt, *args):  # noqa: A002
        pass

    def do_GET(self):  # noqa: N802
        _Handler.requests.append(
            {"t": time.time(), "method": "GET", "path": self.path, "headers": dict(self.headers.items())}
        )
        return super().do_GET()


def http_probe(feed: Path) -> dict:
    _Handler.requests = []
    handler = functools.partial(_Handler, directory=str(feed))
    server = socketserver.TCPServer(("127.0.0.1", 0), handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = update(f"http://127.0.0.1:{port}/", {"apply": False})
    finally:
        server.shutdown()
    beta = PACKAGES / ".betaId"
    result["requests"] = _Handler.requests
    result["betaId_file"] = beta.read_text(encoding="utf-8") if beta.exists() else None
    return result


# ----------------------------------------------------------------------------- scenarios


def cmd_scenarios(args: argparse.Namespace) -> None:
    out = args.out
    out.mkdir(parents=True, exist_ok=True)
    feed = args.feed
    target_nupkg = package(feed, args.target, "full")
    base_nupkg = args.base_nupkg
    which = args.which.split(",")
    results: dict = {"os": "windows" if IS_WIN else "macos", "scenarios": {}}

    def save() -> None:
        (out / "scenarios.json").write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")

    def scenario(name: str, fn) -> None:
        if name not in which:
            return
        print(f"=== scenario {name}", flush=True)
        logdir = out / name
        logdir.mkdir(parents=True, exist_ok=True)
        started = time.time()
        try:
            record = fn(logdir)
        except Exception as exc:  # noqa: BLE001
            record = {"error": repr(exc)}
        record["wall_seconds"] = round(time.time() - started, 1)
        if TRIAL_LOG.exists():
            shutil.copy2(TRIAL_LOG, logdir / "trial.jsonl")
        host_log = Path.home() / ".row-bot" / "launcher-host.log"
        if host_log.exists():
            shutil.copy2(host_log, logdir / "launcher-host.log")
        for server_log in HOME.glob("server-*.log"):
            shutil.copy2(server_log, logdir / server_log.name)
        results["scenarios"][name] = record
        save()
        brief = {k: record.get(k) for k in ("error", "apply_relaunch_seconds", "wall_seconds")}
        print(json.dumps(brief), flush=True)
        kill_everything()

    def seed(kind: str) -> dict:
        PACKAGES.mkdir(parents=True, exist_ok=True)
        dest = PACKAGES / base_nupkg.name
        if kind == "original":
            shutil.copy2(base_nupkg, dest)
            return {"seeded": "original"}
        if kind == "synthesized":
            return {"seeded": "synthesized", **synthesize_base(base_nupkg, dest)}
        return {}

    def basic(logdir: Path, opts: dict, prepare=None, feed_dir: Path = feed, seed_kind: str | None = None) -> dict:
        record = {"install": install(args, logdir)}
        if seed_kind:
            record["seed"] = seed(seed_kind)
        if prepare:
            record["prepare"] = prepare()
        opts = {**opts, "target_version": args.target}
        record["update"] = update(str(feed_dir), opts, timeout_restart=opts.get("restart_timeout", 300))
        record["apply_relaunch_seconds"] = record["update"].get("apply_relaunch_seconds")
        record["tree"] = verify_tree(target_nupkg)
        record["signature"] = code_signature()
        return record

    # macOS installs from the portable zip have no cached base package, so
    # Velopack downloads the full package; most scenarios seed one.
    mac_seed = None if IS_WIN else "original"

    scenario(
        "normal",
        lambda d: basic(d, {"children": True, "server_after_restart": True, "quit_host": False}, seed_kind=None),
    )
    if not IS_WIN:
        scenario(
            "synth_base_quit_host",
            lambda d: basic(d, {"children": True, "server_after_restart": True, "quit_host": True}, seed_kind="synthesized"),
        )
        scenario("seeded_quit_host", lambda d: basic(d, {"children": False, "quit_host": True}, seed_kind="original"))
    else:
        scenario("synth_base", lambda d: basic(d, {"children": False}, prepare=lambda: _win_synth(base_nupkg, args)))

    if IS_WIN:

        def lock_prepare() -> dict:
            target = INSTALL / "app" / "src" / "row_bot" / "__init__.py"
            holder = subprocess.Popen(  # noqa: S603
                [
                    "pwsh",
                    "-NoProfile",
                    "-Command",
                    f"$f=[IO.File]::Open('{target}','Open','Read','None'); Start-Sleep {args.lock_seconds}; $f.Close()",
                ]
            )
            time.sleep(3)
            return {"lock_holder_pid": holder.pid, "file": str(target), "seconds": args.lock_seconds}

        scenario(
            "locked_file",
            lambda d: basic(d, {"children": False, "restart_timeout": args.lock_seconds + 240}, prepare=lock_prepare),
        )

    for mode in ("delta_bytes", "delta_zsdiff_rehash", "delta_newfile_rehash"):
        bad = args.work / f"feed-{mode}"

        def run_bad(logdir: Path, mode: str = mode, bad: Path = bad) -> dict:
            detail = make_bad_feed(feed, bad, args.target, mode)
            record = basic(logdir, {"children": False, "quit_host": True}, feed_dir=bad, seed_kind=mac_seed)
            record["bad_feed"] = detail
            log = record["update"].get("velopack_log", "")
            record["fell_back_to_full"] = "Falling back to full update" in log
            shutil.rmtree(bad, ignore_errors=True)
            return record

        scenario(mode, run_bad)

    def modify_prepare() -> dict:
        app = INSTALL / ("app" if IS_WIN else "Contents/Resources/app")
        (app / "launcher_real.py").write_bytes((app / "launcher_real.py").read_bytes() + b"\n# modified\n")
        victim = next((app / "sounds").rglob("*.*"), None)
        if victim:
            victim.unlink()
        (app / "extra-file.txt").write_text("extra", encoding="utf-8")
        return {"modified": "launcher_real.py", "deleted": str(victim), "added": "extra-file.txt"}

    scenario(
        "modified_install",
        lambda d: basic(d, {"children": False, "quit_host": True}, prepare=modify_prepare, seed_kind=mac_seed),
    )

    def missing_base_prepare() -> dict:
        removed = [p.name for p in PACKAGES.glob("*-full.nupkg")]
        for p in PACKAGES.glob("*-full.nupkg"):
            p.unlink()
        return {"removed": removed}

    def corrupt_base_prepare() -> dict:
        base = package(PACKAGES, args.base, "full")
        data = bytearray(base.read_bytes())
        mid = len(data) // 2
        for i in range(mid, mid + 4096):
            data[i] ^= 0xFF
        base.write_bytes(bytes(data))
        return {"corrupted": base.name}

    if IS_WIN:
        scenario("missing_base", lambda d: basic(d, {"children": False}, prepare=missing_base_prepare))
    scenario(
        "corrupt_base",
        lambda d: basic(d, {"children": False, "quit_host": True}, prepare=corrupt_base_prepare, seed_kind=mac_seed),
    )

    def run_probe(logdir: Path) -> dict:
        record = {"install": install(args, logdir)}
        with NetSampler() as net:
            record["probe"] = http_probe(feed)
        record["mac_lsof_non_loopback"] = net.seen
        return record

    scenario("http_probe", run_probe)

    if "uninstall" in which:
        install(args, out)
        results["scenarios"]["uninstall"] = uninstall()
        save()
    save()


def _win_synth(base_nupkg: Path, args: argparse.Namespace) -> dict:
    removed = [p.name for p in PACKAGES.glob("*-full.nupkg")]
    for p in PACKAGES.glob("*-full.nupkg"):
        p.unlink()
    info = synthesize_base(base_nupkg, PACKAGES / base_nupkg.name)
    return {"removed": removed, **info}


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("sizes")
    p.add_argument("--feed", type=Path, required=True)
    p.add_argument("--variant", required=True)
    p.add_argument("--base", default="5.0.0")
    p.add_argument("--target", default="5.1.0")
    p.add_argument("--installers", type=Path, nargs="*", default=[])
    p.add_argument("--extra-delta", action="append")
    p.add_argument("--extra-diff", action="append")
    p.add_argument("--payload-reports", nargs="*")
    p.add_argument("--out", type=Path, required=True)
    p = sub.add_parser("scenarios")
    p.add_argument("--feed", type=Path, required=True)
    p.add_argument("--base", default="5.0.0")
    p.add_argument("--target", default="5.1.0")
    p.add_argument("--base-nupkg", type=Path, required=True)
    p.add_argument("--setup", type=Path)
    p.add_argument("--portable", type=Path)
    p.add_argument("--work", type=Path, required=True)
    p.add_argument("--lock-seconds", type=int, default=60)
    p.add_argument(
        "--which",
        default="normal,synth_base,synth_base_quit_host,seeded_quit_host,locked_file,delta_bytes,"
        "delta_zsdiff_rehash,delta_newfile_rehash,modified_install,missing_base,corrupt_base,http_probe,uninstall",
    )
    p.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.cmd == "sizes":
        cmd_sizes(args)
    else:
        cmd_scenarios(args)


if __name__ == "__main__":
    sys.exit(main())
