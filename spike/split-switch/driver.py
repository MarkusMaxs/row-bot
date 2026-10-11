"""Split-and-switch trial driver (Phase 1 spike, CI only).

Installs 5.0.0 in the PLAN.md layout, starts the real 5.0.0 launcher (server
child + a stand-in window process) through a native-launcher stand-in, then
stages 5.1.0 next to it in several ways and switches to it, measuring
everything. Results go to <out>/results.json after every scenario.

  python driver.py --os windows|macos|linux --payload <dir> --parts <dir> --uv <uv> --work <dir> --out <dir> --config run.json
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import functools
import http.server
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import engine  # noqa: E402
import netproxy  # noqa: E402
import psutil  # noqa: E402

IS_WIN = os.name == "nt"
IS_MAC = sys.platform == "darwin"
PORT = 8195
BASE = "5.0.0"
TARGET = "5.1.0"
WINDOW_STANDIN = "import time; time.sleep(86400)  # trial window stand-in"


def iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def sh(cmd: list, timeout: float = 600, **kw) -> subprocess.CompletedProcess:
    return subprocess.run([str(c) for c in cmd], capture_output=True, text=True, timeout=timeout, **kw)  # noqa: S603


def http_status(url: str, timeout: float = 1.0) -> int | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310 - loopback only
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except Exception:  # noqa: BLE001
        return None


def norm(path: Path | str) -> str:
    text = str(Path(path).resolve()) if Path(path).exists() else str(path)
    return text.lower() if IS_WIN else text


class PartsServer:
    """Serves the parts folder on loopback: stands in for the GitHub release download."""

    def __init__(self, folder: Path) -> None:
        handler = functools.partial(_QuietHandler, directory=str(folder))
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, fmt, *args):  # noqa: A002, ANN001
        pass


class Trial:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.os = args.os
        self.out = args.out.resolve()
        self.work = args.work.resolve()
        self.out.mkdir(parents=True, exist_ok=True)
        self.work.mkdir(parents=True, exist_ok=True)
        self.config = json.loads(args.config.read_text(encoding="utf-8")) if args.config else {}
        self.parts = args.parts.resolve()
        self.manifest = json.loads((self.parts / f"row-bot-update-{TARGET}.json").read_text(encoding="utf-8"))
        self.uv = args.uv.resolve() if args.uv else None
        self.data = self.work / "rowbot-data"
        self.data.mkdir(parents=True, exist_ok=True)
        self.results: dict = {"os": self.os, "started": iso(), "platform": self.platform_info(), "scenarios": {}}
        self.parts_server = PartsServer(self.parts)
        self.proxy = netproxy.CountingProxy().start()
        self.host_proc: subprocess.Popen | None = None
        self.window_proc: subprocess.Popen | None = None
        self.label_n = 0
        self.base_manifest: dict | None = None
        self.base_inodes: set[int] = set()
        self.bundle = args.bundle.resolve() if args.bundle else None
        self.real_window = False
        if IS_WIN:
            # CTRL_BREAK needs a console shared with the launcher; no-op if we have one.
            import ctypes

            self.results["alloc_console"] = bool(ctypes.windll.kernel32.AllocConsole())

    # ------------------------------------------------------------------ bookkeeping

    def platform_info(self) -> dict:
        info = {
            "python": sys.version,
            "platform": sys.platform,
            "machine": os.uname().machine if hasattr(os, "uname") else os.environ.get("PROCESSOR_ARCHITECTURE"),
            "cpus": os.cpu_count(),
        }
        if IS_MAC:
            info["sw_vers"] = sh(["sw_vers"]).stdout.strip()
        return info

    def save(self) -> None:
        self.results["updated"] = iso()
        (self.out / "results.json").write_text(json.dumps(self.results, indent=2, default=str), encoding="utf-8")

    def scenario(self, name: str, fn, *a, **kw) -> dict:  # noqa: ANN001
        print(f"\n===== {name} ({iso()})", flush=True)
        started = time.time()
        try:
            result = fn(*a, **kw)
        except Exception as exc:  # noqa: BLE001
            result = {"error": repr(exc), "traceback": traceback.format_exc()[-3000:]}
        result = result if isinstance(result, dict) else {"result": result}
        result["scenario_seconds"] = round(time.time() - started, 1)
        self.results["scenarios"][name] = result
        self.save()
        print(json.dumps(_brief(result), indent=1, default=str)[:4000], flush=True)
        return result

    def next_label(self) -> str:
        self.label_n += 1
        return f"{TARGET}-s{self.label_n}"

    # ------------------------------------------------------------------ layout

    def install_base(self) -> dict:
        payload = self.args.payload.resolve() if self.args.payload else None
        started = time.time()
        if self.os == "windows":
            self.root = Path(os.environ["LOCALAPPDATA"]) / "Programs" / "RowBotSplitTrial"
            dst = self.root / "versions" / BASE
            for part in ("python", "app"):
                rc = subprocess.run(["robocopy", str(payload / part), str(dst / part), "/E", "/MT:16", "/NFL", "/NDL", "/NJH", "/NJS", "/NP"]).returncode
                if rc >= 8:
                    raise RuntimeError(f"robocopy {part} failed rc={rc}")
            self.layout = engine.Layout(self.root, "windows")
            self.launcher_args = ["--server", "--no-open", "--no-splash", "--no-ollama", "--port", str(PORT)]
        elif self.os == "linux":
            xdg = Path.home() / "rbtrial-xdg"
            env = dict(os.environ, XDG_DATA_HOME=str(xdg), ROW_BOT_SUPPRESS_INSTALL_PATH_HINT="1")
            proc = sh(["bash", payload / "install.sh"], env=env)
            if proc.returncode != 0:
                raise RuntimeError(proc.stdout + proc.stderr)
            self.root = xdg / "row-bot"
            self.layout = engine.Layout(self.root, "linux")
            self.launcher_args = ["serve", "--port", str(PORT)]
        else:
            self.root = Path.home() / "Library" / "Application Support" / "RowBotSplitTrial"
            self.root.mkdir(parents=True, exist_ok=True)
            seed = self.bundle / "Contents" / "Resources"
            self.layout = engine.Layout(self.root, "macos", seed=seed, seed_version=BASE)
            # pywebview runs on the macOS runner: use the real native window, not a stand-in.
            self.launcher_args = ["--no-tray", "--native", "--no-splash", "--no-ollama", "--port", str(PORT)]
            self.real_window = True
            (self.root / "host-args.json").write_text(json.dumps(["launcher.py", *self.launcher_args]), encoding="utf-8")
        engine.write_state(self.root, engine.new_state(BASE))
        base = self.layout.version_root(BASE)
        out = {
            "root": str(self.root),
            "base_root": str(base),
            "install_seconds": round(time.time() - started, 1),
            "python": engine.dir_stats(base / "python"),
            "app": engine.dir_stats(base / "app"),
            "assets_content_sha256": engine.content_hash(base / "app"),
            "manifest_assets_content_sha256": self.manifest["assets"]["content_sha256"],
        }
        t = time.time()
        self.base_manifest = {p: engine.tree_manifest(base / p) for p in ("python", "app")}
        out["hash_seconds"] = round(time.time() - t, 1)
        self.base_inodes = engine.inode_set(base)
        return out

    def base_integrity(self) -> dict:
        base = self.layout.version_root(BASE)
        t = time.time()
        res = {p: engine.compare_manifests(self.base_manifest[p], engine.tree_manifest(base / p)) for p in ("python", "app")}
        res["ok"] = all(r["content_changed"] == 0 and r["removed"] == 0 for r in res.values())
        res["seconds"] = round(time.time() - t, 1)
        return res

    # ------------------------------------------------------------------ processes

    def processes_under(self, root: Path) -> list[dict]:
        prefix = norm(root)
        out = []
        for proc in psutil.process_iter(["pid", "ppid", "exe", "cmdline"]):
            exe = proc.info.get("exe") or ""
            if exe and norm(exe).startswith(prefix):
                out.append({"pid": proc.info["pid"], "ppid": proc.info["ppid"], "exe": exe, "cmd": " ".join(proc.info.get("cmdline") or [])[:200]})
        return out

    def launcher_of(self, root: Path) -> psutil.Process | None:
        rows = [r for r in self.processes_under(root / "python") if "launcher.py" in r["cmd"] or (self.os == "linux" and " serve" in r["cmd"])]
        pids = {r["pid"] for r in rows}
        tops = [r for r in rows if r["ppid"] not in pids]
        return psutil.Process(tops[0]["pid"]) if tops else None

    def port_owner(self, port: int = PORT) -> list[dict]:
        pids: set[int] = set()
        if IS_MAC:
            out = sh(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"]).stdout
            pids = {int(x) for x in out.split() if x.strip().isdigit()}
        else:
            for conn in psutil.net_connections("tcp"):
                if conn.laddr and conn.laddr.port == port and conn.status == psutil.CONN_LISTEN and conn.pid:
                    pids.add(conn.pid)
        rows = []
        for pid in sorted(pids):
            try:
                proc = psutil.Process(pid)
                rows.append({"pid": pid, "exe": proc.exe(), "cmd": " ".join(proc.cmdline())[:200]})
            except psutil.Error as exc:
                rows.append({"pid": pid, "error": repr(exc)})
        return rows

    def wait_ready(self, timeout: float, root: Path | None = None) -> float | None:
        started = time.time()
        while time.time() - started < timeout:
            if http_status(f"http://127.0.0.1:{PORT}/readyz") == 200:
                if root is None:
                    return round(time.time() - started, 2)
                owners = self.port_owner()
                if owners and all(norm(o.get("exe", "")).startswith(norm(root)) for o in owners):
                    return round(time.time() - started, 2)
            time.sleep(0.1)
        return None

    def graceful_stop(self, pid: int) -> str:
        try:
            if IS_WIN:
                os.kill(pid, signal.CTRL_BREAK_EVENT)
                return "CTRL_BREAK_EVENT"
            os.kill(pid, signal.SIGTERM)
            return "SIGTERM"
        except OSError as exc:
            psutil.Process(pid).terminate()
            return f"terminate (graceful signal failed: {exc!r})"

    def start_window(self) -> None:
        if self.real_window:
            return
        state = engine.read_state(self.root) or {}
        vroot = self.layout.version_root(state.get("active", BASE)) if self.os != "linux" else self.root / "releases" / state.get("active", BASE)
        exe = engine.Layout.python_exe(vroot, gui=True)
        self.window_proc = subprocess.Popen([str(exe), "-c", WINDOW_STANDIN], env=engine.runtime_env(vroot, self.data))  # noqa: S603

    def stop_window(self) -> dict:
        if not self.window_proc or self.window_proc.poll() is not None:
            return {"running": False}
        started = time.time()
        self.window_proc.terminate()
        try:
            self.window_proc.wait(10)
        except subprocess.TimeoutExpired:
            self.window_proc.kill()
        return {"running": True, "stop_seconds": round(time.time() - started, 2)}

    def mac_host_pids(self) -> list[int]:
        exe = norm(self.bundle / "Contents" / "MacOS")
        pids = []
        for proc in psutil.process_iter(["pid", "exe", "cmdline"]):
            if norm(proc.info.get("exe") or "").startswith(exe) and "--trial-probe" not in " ".join(proc.info.get("cmdline") or []):
                pids.append(proc.info["pid"])
        return pids

    def start_app(self) -> dict:
        started = time.time()
        if IS_MAC:
            proc = sh(["open", "-n", "-a", self.bundle])
            launched = {"open_rc": proc.returncode, "stderr": proc.stderr[-500:]}
        else:
            cmd = [sys.executable, str(HERE / "host.py"), "--root", str(self.root), "--layout", self.os, "--port", str(PORT),
                   "--log", str(self.out / "host.jsonl"), "--data-dir", str(self.data), "--", *self.launcher_args]
            self.host_proc = subprocess.Popen(cmd)  # noqa: S603 - inherits our console (CTRL_BREAK)
            launched = {"host_pid": self.host_proc.pid}
        ready = self.wait_ready(420)
        self.start_window()
        state = engine.read_state(self.root) or {}
        active_root = self.active_root()
        return {**launched, "ready_seconds": ready, "total_seconds": round(time.time() - started, 2), "active": state.get("active"),
                "port_owner": self.port_owner(), "app_processes": self.processes_under(active_root / "python"),
                "loaded_native": self.loaded_native()}

    def active_root(self) -> Path:
        state = engine.read_state(self.root) or {}
        active = state.get("active", BASE)
        if self.os == "linux":
            return self.root / "releases" / active
        return self.layout.version_root(active)

    def loaded_native(self) -> dict:
        """Native files the running server has mapped (Windows/Linux), to see whether a
        changed package's extension is in use while the clone is synced."""
        out: dict = {}
        for owner in self.port_owner():
            try:
                maps = [m.path for m in psutil.Process(owner["pid"]).memory_maps()]
            except Exception as exc:  # noqa: BLE001
                out["error"] = repr(exc)
                continue
            site = [p for p in maps if "site-packages" in p.lower()]
            out["mapped_files"] = len(maps)
            out["site_packages_native"] = len(site)
            out["changed_packages_mapped"] = [p for p in site if any(k in p.lower() for k in ("multidict", "pypdf", "hydra", "langgraph"))]
        return out

    def stop_app(self) -> dict:
        state = engine.read_state(self.root) or {}
        state.pop("restart", None)
        engine.write_state(self.root, state)
        out = {"window": self.stop_window()}
        root = self.active_root()
        launcher = self.launcher_of(root)
        if launcher:
            out["signal"] = self.graceful_stop(launcher.pid)
            gone, alive = psutil.wait_procs([launcher], timeout=90)
            out["launcher_exited"] = bool(gone)
            for proc in alive:
                proc.kill()
        if self.host_proc:
            try:
                self.host_proc.wait(60)
            except subprocess.TimeoutExpired:
                self.host_proc.kill()
            self.host_proc = None
        if IS_MAC:
            for pid in self.mac_host_pids():
                with contextlib.suppress(OSError):
                    os.kill(pid, signal.SIGTERM)
            time.sleep(2)
        time.sleep(1)
        out["left"] = self.processes_under(root / "python")
        for row in out["left"]:
            with contextlib.suppress(psutil.Error):
                psutil.Process(row["pid"]).kill()
        return out

    # ------------------------------------------------------------------ switch

    def switch(self, to: str) -> dict:
        state = engine.read_state(self.root) or engine.new_state(BASE)
        frm = state["active"]
        old_root = self.active_root()
        new_root = self.root / "releases" / to if self.os == "linux" else self.layout.version_root(to)
        launcher = self.launcher_of(old_root)
        if launcher is None:
            return {"error": "launcher not found", "processes": self.processes_under(old_root)}
        tree = [p.pid for p in launcher.children(recursive=True)]
        host_log_offset = (self.out / "host.jsonl").stat().st_size if (self.out / "host.jsonl").exists() else 0
        t0 = time.time()
        # The app's apply step: flip the pointer, stop children gracefully, exit.
        state.update(active=to, previous=frm, ready=None, pending_verification=True, failed_starts=0, restart=True)
        if self.os == "linux":
            tmp = self.root / ".current.switch"
            if tmp.is_symlink():
                tmp.unlink()
            os.symlink(f"releases/{to}", tmp)
            os.replace(tmp, self.root / "current")
        engine.write_state(self.root, state)
        t_state = time.time()
        window = self.stop_window()
        t_window = time.time()
        how = self.graceful_stop(launcher.pid)
        gone, alive = psutil.wait_procs([launcher], timeout=90)
        t_exit = time.time()
        killed = [p.pid for p in alive]
        for proc in alive:
            proc.kill()
        children_alive = [pid for pid in tree if psutil.pid_exists(pid)]
        ready = self.wait_ready(420, root=new_root)
        t_ready = time.time()
        owners = self.port_owner()
        leftovers = self.processes_under(old_root / "python")
        verified_at = None
        deadline = time.time() + 30
        while time.time() < deadline:
            current = engine.read_state(self.root) or {}
            if not current.get("pending_verification"):
                verified_at = time.time()
                break
            time.sleep(0.1)
        self.start_window()
        host_events = self.host_events(host_log_offset)
        spawned = [e for e in host_events if e.get("event") in {"spawned"}]
        return {
            "from": frm,
            "to": to,
            "signal": how,
            "launcher_exited_gracefully": bool(gone) and not killed,
            "killed_after_90s": killed,
            "server_tree_alive_after_launcher_exit": children_alive,
            "window": window,
            "seconds": {
                "write_state": round(t_state - t0, 3),
                "stop_window": round(t_window - t_state, 2),
                "app_exit": round(t_exit - t_window, 2),
                "exit_to_new_server_ready": round(t_ready - t_exit, 2) if ready is not None else None,
                "request_to_new_server_ready": round(t_ready - t0, 2) if ready is not None else None,
                "exit_to_spawn": round(spawned[-1]["t"] - t_exit, 2) if spawned else None,
                "verified_after_ready": round(verified_at - t_ready, 2) if verified_at and ready is not None else None,
            },
            "new_server_ready": ready is not None,
            "port_owner": owners,
            "port_owner_in_new_version": bool(owners) and all(norm(o.get("exe", "")).startswith(norm(new_root)) for o in owners),
            "old_version_processes_left": leftovers,
            "state_after": engine.read_state(self.root),
            "launcher_state": self.launcher_state(),
            "host_events": host_events[-12:],
        }

    def host_events(self, offset: int) -> list[dict]:
        if IS_MAC:
            path = self.root / "host.log"
            rows = []
            if path.exists():
                for line in path.read_text(encoding="utf-8", errors="replace").splitlines()[-40:]:
                    parts = line.split(" ", 2)
                    with contextlib.suppress(ValueError, IndexError):
                        rows.append({"t": float(parts[0]), "event": parts[2].split(" ", 1)[0], "line": parts[2][:200]})
            return rows
        path = self.out / "host.jsonl"
        if not path.exists():
            return []
        with path.open("rb") as handle:
            handle.seek(offset)
            return [json.loads(line) for line in handle.read().decode("utf-8", errors="replace").splitlines() if line.strip()]

    def launcher_state(self) -> dict | None:
        path = self.data / "launcher_state.json"
        if IS_MAC:
            path = self.root / "data" / "launcher_state.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return {k: data.get(k) for k in ("mode", "requested_mode", "selected_mode", "opened_mode", "port", "phase", "owns_server", "pid")}

    # ------------------------------------------------------------------ staging

    def stage(self, tool: str, source: str, clone: str, *, prune: bool = False, inject: str | None = None, switch: bool = True,
              parts_base: str | None = None, stop_after: str | None = None, lock_version: str = TARGET,
              force_assets: bool = False, delete_after: bool = False) -> dict:
        label = self.next_label()
        self.proxy.reset()
        rep = engine.stage(
            layout=self.layout,
            current=BASE,
            new=label,
            expect_version=TARGET,
            manifest=self.manifest,
            parts_base=parts_base or self.parts_server.url,
            pylock=self.parts / f"pylock-{lock_version}.toml",
            export=self.parts / f"requirements-hashed-{lock_version}.txt",
            clone_method=clone,
            tool=tool,
            source=source,
            uv=self.uv,
            proxy=self.proxy.url,
            log=self.out / f"stage-{label}.log",
            prune=prune,
            inject=inject,
            force_assets=force_assets,
            stop_after=stop_after,
            linux_shell_from=(self.root / "releases" / BASE) if self.os == "linux" else None,
        )
        rep["label"] = label
        net = self.proxy.snapshot()
        allowed = {"pypi.org", "files.pythonhosted.org", "download.pytorch.org"}
        net["hosts_outside_allow_list"] = sorted(h for h in net["hosts"] if h not in allowed)
        rep["network"] = net
        ready_name = rep.get("ready_version", label)
        final = self.layout.versions / ready_name
        if rep.get("ok") and final.exists():
            rep["staged_size"] = engine.dir_stats(final, exclude_inodes=self.base_inodes)
        if clone in {"hardlink", "cp-c", "clonefile"} or inject:
            rep["base_integrity"] = self.base_integrity()
        rep["state_after_stage"] = engine.read_state(self.root)
        if rep.get("ok") and switch and stop_after is None:
            rep["switch"] = self.switch(ready_name)
            if IS_MAC:
                rep["mac_after_switch"] = self.mac_checks(ready_name)
            rep["switch_back"] = self.switch(BASE)
        if delete_after and final.exists():
            rep["delete"] = engine.rmtree(final)
        return rep

    # ------------------------------------------------------------------ scenarios

    def clone_only(self, method: str) -> dict:
        base = self.layout.version_root(BASE)
        dst = self.layout.versions / f"clone-test-{method}"
        if dst.exists():
            engine.rmtree(dst)
        free = shutil.disk_usage(self.root).free
        rep = engine.clone_tree(base / "python", dst / "python", method)
        rep["disk_free_delta_bytes"] = free - shutil.disk_usage(self.root).free
        rep["size"] = engine.dir_stats(dst, exclude_inodes=self.base_inodes)
        # Can a folder full of hard links to files the running app has mapped be renamed?
        t = time.time()
        try:
            renamed = dst.with_name(dst.name + "-renamed")
            os.replace(dst, renamed)
            rep["rename"] = {"ok": True, "seconds": round(time.time() - t, 3)}
            dst = renamed
        except OSError as exc:
            rep["rename"] = {"ok": False, "error": repr(exc)}
        rep["delete"] = engine.rmtree(dst)
        return rep

    def rollback_rule(self) -> dict:
        out: dict = {"stop": self.stop_app()}
        label = "9.9.9-broken"
        base = self.layout.version_root(BASE)
        vroot = self.layout.versions / label
        if vroot.exists():
            engine.rmtree(vroot)
        method = "clonefile" if IS_MAC else "hardlink"
        engine.clone_tree(base / "python", vroot / "python", method)
        shutil.copytree(base / "app", vroot / "app", symlinks=True)
        (vroot / "app" / "launcher.py").write_text("import sys\nsys.exit(3)  # trial: this version cannot start\n", encoding="utf-8")
        if self.os == "linux":
            for name in ("bin", "share", "install.sh", "uninstall.sh", "install_info.json"):
                src = base / name
                if src.is_dir():
                    shutil.copytree(src, vroot / name, symlinks=True)
                elif src.exists():
                    shutil.copy2(src, vroot / name)
            tmp = self.root / ".current.rb"
            if tmp.is_symlink():
                tmp.unlink()
            os.symlink(f"releases/{label}", tmp)
            os.replace(tmp, self.root / "current")
        state = engine.new_state(label)
        state.update(previous=BASE, pending_verification=True, failed_starts=0)
        engine.write_state(self.root, state)
        out["state_before"] = state
        offset = (self.out / "host.jsonl").stat().st_size if (self.out / "host.jsonl").exists() else 0
        started = time.time()
        out["start"] = self.start_app()
        deadline = time.time() + 300
        while time.time() < deadline:
            current = engine.read_state(self.root) or {}
            if current.get("active") == BASE and not current.get("pending_verification") and http_status(f"http://127.0.0.1:{PORT}/readyz") == 200:
                break
            time.sleep(0.25)
        out["seconds_to_running_previous"] = round(time.time() - started, 1)
        out["state_after"] = engine.read_state(self.root)
        out["host_events"] = [{k: e.get(k) for k in ("event", "version", "failed_starts", "rc", "reason", "line")} for e in self.host_events(offset)]
        out["port_owner"] = self.port_owner()
        if self.os == "linux":
            out["current_link"] = os.readlink(self.root / "current")
        out["ok"] = (out["state_after"] or {}).get("active") == BASE and out["port_owner"] != []
        return out

    def cleanup_old(self) -> dict:
        """Delete every non-active staged version while the app runs, then again after it stopped."""
        state = engine.read_state(self.root) or {}
        keep = {state.get("active"), BASE}
        labels = [p for p in self.layout.versions.iterdir() if p.is_dir() and p.name not in keep]
        out: dict = {"while_running": {}, "after_stop": {}}
        for path in labels:
            out["while_running"][path.name] = engine.rmtree(path, attempts=3)
        out["stop"] = self.stop_app()
        for path in labels:
            if path.exists():
                out["after_stop"][path.name] = engine.rmtree(path, attempts=3)
        return out

    def defender(self, enable: bool) -> dict:
        def ps(script: str) -> str:
            return sh(["pwsh", "-NoProfile", "-Command", script], timeout=300).stdout.strip()

        status_cmd = (
            "Get-MpComputerStatus | Select-Object AMRunningMode,AMServiceEnabled,AntivirusEnabled,RealTimeProtectionEnabled,"
            "OnAccessProtectionEnabled,IoavProtectionEnabled,BehaviorMonitorEnabled,AMProductVersion | ConvertTo-Json"
        )
        prefs_cmd = (
            "Get-MpPreference | Select-Object DisableRealtimeMonitoring,DisableBehaviorMonitoring,DisableIOAVProtection,"
            "DisableScriptScanning,DisableArchiveScanning,ExclusionPath,ExclusionProcess,ExclusionExtension | ConvertTo-Json"
        )
        out = {"before_status": _json(ps(status_cmd)), "before_prefs": _json(ps(prefs_cmd))}
        if enable:
            out["set"] = ps(
                "Set-MpPreference -DisableRealtimeMonitoring $false -DisableBehaviorMonitoring $false -DisableIOAVProtection $false "
                "-DisableScriptScanning $false -DisableArchiveScanning $false; "
                "$p = Get-MpPreference; "
                "if ($p.ExclusionPath) { Remove-MpPreference -ExclusionPath $p.ExclusionPath }; "
                "if ($p.ExclusionProcess) { Remove-MpPreference -ExclusionProcess $p.ExclusionProcess }; "
                "if ($p.ExclusionExtension) { Remove-MpPreference -ExclusionExtension $p.ExclusionExtension }; 'done'"
            )
            time.sleep(5)
        out["after_status"] = _json(ps(status_cmd))
        out["after_prefs"] = _json(ps(prefs_cmd))
        out["eicar"] = self.eicar_probe()
        return out

    def eicar_probe(self) -> dict:
        """Write the standard AV test string; real-time protection removes or blocks it."""
        key = 0x5A
        data = bytes(b ^ key for b in base64.b64decode(_EICAR_XOR))
        path = self.root / "av-probe.txt"
        try:
            path.write_bytes(data)
            wrote = True
        except OSError as exc:
            return {"write_blocked": repr(exc)}
        time.sleep(5)
        try:
            readable = path.read_bytes() == data
        except OSError as exc:
            readable = f"blocked: {exc!r}"
        exists = path.exists()
        with contextlib.suppress(OSError):
            path.unlink()
        return {"wrote": wrote, "exists_after_5s": exists, "readable_after_5s": readable, "rtp_effective": (not exists) or readable is not True}

    def mac_checks(self, label: str | None) -> dict:
        out: dict = {}
        cs = sh(["codesign", "--verify", "--deep", "--strict", "--verbose=2", self.bundle])
        out["codesign_bundle"] = {"rc": cs.returncode, "out": (cs.stdout + cs.stderr)[-600:]}
        sp = sh(["spctl", "--assess", "--type", "execute", "-vv", self.bundle])
        out["spctl_bundle"] = {"rc": sp.returncode, "out": (sp.stdout + sp.stderr)[-600:]}
        probe = self.out / f"probe-{label or 'seed'}-{int(time.time())}.json"
        started = time.time()
        proc = sh(["open", "-n", "-W", "-a", self.bundle, "--args", "--trial-probe", str(probe)], timeout=600)
        out["probe_open_rc"] = proc.returncode
        out["probe_seconds"] = round(time.time() - started, 1)
        try:
            out["probe"] = json.loads(probe.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log = Path(str(probe) + ".log")
            out["probe_error"] = repr(exc)
            out["probe_log"] = log.read_text(encoding="utf-8", errors="replace")[-2000:] if log.exists() else None
        launcher = self.launcher_of(self.active_root())
        if launcher:
            info = sh(["sudo", "-n", "launchctl", "procinfo", str(launcher.pid)])
            lines = [line.strip() for line in info.stdout.splitlines() if "responsible" in line]
            out["launcher_procinfo_responsible"] = lines[:6] or info.stderr[-300:]
        if label:
            vroot = self.layout.versions / label
            py = sorted((vroot / "python" / "bin").glob("python3.*"))
            target = py[0] if py else vroot / "python" / "bin" / "python3"
            sig = sh(["codesign", "-dv", "--verbose=2", target])
            out["staged_python_signature"] = [line for line in sig.stderr.splitlines() if line.startswith(("Authority=", "TeamIdentifier", "Runtime", "Identifier", "CodeDirectory"))]
            changed = sorted((engine.Layout.site_packages(vroot) / "multidict").glob("*.so"))
            if changed:
                sig = sh(["codesign", "-dv", "--verbose=2", changed[0]])
                out["new_wheel_so_signature"] = {"file": changed[0].name, "info": sig.stderr.strip().splitlines()[:6]}
        return out


# The standard 68-byte antivirus test string, XOR 0x5A, base64 (never stored in clear).
_EICAR_XOR = "Am8Vewp/GhsKAW4GCgACb25yCgRzbRkZc20nfh8TGRsIdwkOGxQeGwgedxsUDhMMEwgPCXcOHwkOdxwTFh97fhJxEnA="


def _json(text: str):  # noqa: ANN202
    try:
        return json.loads(text)
    except ValueError:
        return text[-1000:]


def _brief(result: dict) -> dict:
    keep = {}
    for key, value in result.items():
        if key in {"plan", "host_events", "traceback"}:
            continue
        if isinstance(value, dict) and len(json.dumps(value, default=str)) > 1500:
            keep[key] = {k: v for k, v in value.items() if not isinstance(v, (dict, list))}
        else:
            keep[key] = value
    return keep


# ---------------------------------------------------------------------------- linux extras


def linux_launch_mode(trial: Trial) -> dict:
    """Today's tarball relaunch (updater.py ~l.847-853) starts bin/row-bot with no
    arguments: a `serve --port N` install comes back in browser mode."""
    out: dict = {"stop": trial.stop_app()}
    data = trial.work / "rowbot-data-relaunch"
    data.mkdir(exist_ok=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}
    env.update(ROW_BOT_DATA_DIR=str(data), BROWSER="/bin/true", PYTHONDONTWRITEBYTECODE="1")
    launcher = trial.root / "current" / "bin" / "row-bot"
    proc = subprocess.Popen([str(launcher)], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)  # noqa: S603
    deadline = time.time() + 240
    state = None
    while time.time() < deadline:
        try:
            state = json.loads((data / "launcher_state.json").read_text(encoding="utf-8"))
            if state.get("phase") == "running":
                break
        except (OSError, ValueError):
            pass
        time.sleep(0.5)
    out["todays_relaunch_without_args"] = {k: (state or {}).get(k) for k in ("mode", "requested_mode", "selected_mode", "opened_mode", "port", "phase")}
    out["todays_relaunch_cmdline"] = " ".join(psutil.Process(proc.pid).cmdline()) if psutil.pid_exists(proc.pid) else None
    os.kill(proc.pid, signal.SIGTERM)
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(60)
    out["restart_trial_host"] = trial.start_app()
    return out


def linux_systemd(trial: Trial, label: str) -> dict:
    """Under systemd the service manager restarts: flip `current`, stop the main
    process gracefully, and Restart=always brings up the new release."""
    out: dict = {"stop": trial.stop_app()}
    unit = "rowbot-trial"
    port = PORT
    exec_path = trial.root / "current" / "bin" / "row-bot"
    tmp = trial.root / ".current.sd"
    if tmp.is_symlink():
        tmp.unlink()
    os.symlink(f"releases/{BASE}", tmp)
    os.replace(tmp, trial.root / "current")
    state = engine.read_state(trial.root) or engine.new_state(BASE)
    state.update(active=BASE, pending_verification=False, restart=False)
    engine.write_state(trial.root, state)
    cmd = ["sudo", "-n", "systemd-run", f"--unit={unit}", f"--uid={os.getuid()}", f"--gid={os.getgid()}",
           f"--setenv=HOME={Path.home()}", f"--setenv=ROW_BOT_DATA_DIR={trial.data}", "--setenv=BROWSER=/bin/true",
           "--property=Restart=always", "--property=RestartSec=200ms", "--property=KillMode=control-group",
           str(exec_path), "serve", "--port", str(port)]
    run = sh(cmd)
    out["systemd_run"] = {"rc": run.returncode, "out": (run.stdout + run.stderr)[-600:]}
    out["ready_first"] = trial.wait_ready(300, root=trial.root / "releases" / BASE)
    main_pid = sh(["systemctl", "show", "-p", "MainPID", "--value", unit]).stdout.strip()
    out["main_pid_before"] = main_pid
    t0 = time.time()
    tmp = trial.root / ".current.sd"
    os.symlink(f"releases/{label}", tmp)
    os.replace(tmp, trial.root / "current")
    state.update(active=label, previous=BASE, pending_verification=True)
    engine.write_state(trial.root, state)
    sig = sh(["sudo", "-n", "systemctl", "kill", "--kill-whom=main", "--signal=SIGTERM", unit])
    out["kill_rc"] = sig.returncode
    ready = trial.wait_ready(300, root=trial.root / "releases" / label)
    out["request_to_new_server_ready"] = round(time.time() - t0, 2) if ready is not None else None
    out["main_pid_after"] = sh(["systemctl", "show", "-p", "MainPID", "--value", unit]).stdout.strip()
    out["n_restarts"] = sh(["systemctl", "show", "-p", "NRestarts", "--value", unit]).stdout.strip()
    out["port_owner"] = trial.port_owner()
    out["launcher_state"] = trial.launcher_state()
    out["old_processes_left"] = trial.processes_under(trial.root / "releases" / BASE / "python")
    out["journal_tail"] = sh(["sudo", "-n", "journalctl", "-u", unit, "--no-pager", "-n", "15", "-o", "short-iso"]).stdout[-2500:]
    stop = sh(["sudo", "-n", "systemctl", "stop", unit])
    out["unit_stop_rc"] = stop.returncode
    tmp = trial.root / ".current.sd"
    os.symlink(f"releases/{BASE}", tmp)
    os.replace(tmp, trial.root / "current")
    state.update(active=BASE, previous=label, pending_verification=False)
    engine.write_state(trial.root, state)
    out["restart_trial_host"] = trial.start_app()
    return out


# ---------------------------------------------------------------------------- main


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--os", choices=["windows", "macos", "linux"], required=True)
    parser.add_argument("--payload", type=Path)
    parser.add_argument("--bundle", type=Path, help="installed trial Row-Bot.app (macOS)")
    parser.add_argument("--parts", type=Path, required=True)
    parser.add_argument("--uv", type=Path)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--phase", choices=["main", "defender"], default="main")
    args = parser.parse_args()
    trial = Trial(args)
    cfg = trial.config
    only = set(cfg.get("only") or [])

    def want(name: str) -> bool:
        return not only or name in only

    clone_methods = {"windows": ["hardlink", "copy"], "linux": ["hardlink", "copy"], "macos": ["clonefile", "cp-c", "copy"]}[args.os]
    fast = "clonefile" if IS_MAC else "hardlink"

    if "error" in trial.scenario("install_base", trial.install_base):
        trial.save()
        return 1
    if trial.scenario("start_base", trial.start_app).get("ready_seconds") is None:
        trial.save()
        return 1
    if IS_MAC:
        trial.scenario("mac_seed_checks", trial.mac_checks, None)
    for method in clone_methods:
        if want("clone"):
            trial.scenario(f"clone_{method}", trial.clone_only, method)
    for tool, source in (("uv", "plan"), ("pip", "plan"), ("uv", "export"), ("pip", "export")):
        if want(f"stage_{tool}_{source}"):
            trial.scenario(f"stage_{tool}_{source}_{fast}", trial.stage, tool, source, fast, switch=True)
    if want("prune"):
        trial.scenario(f"stage_uv_plan_{fast}_prune", trial.stage, "uv", "plan", fast, prune=True, switch=True)
    if want("copy_baseline"):
        trial.scenario("stage_uv_plan_copy", trial.stage, "uv", "plan", "copy", switch=False, delete_after=True)
    if want("assets"):
        trial.scenario(f"stage_uv_plan_{fast}_assets_forced", trial.stage, "uv", "plan", fast, switch=False, force_assets=True)
    if want("failures"):
        bad = trial.work / "parts-corrupt"
        bad.mkdir(exist_ok=True)
        code = trial.parts / trial.manifest["code"]["file"]
        blob = bytearray(code.read_bytes())
        blob[len(blob) // 2] ^= 0xFF
        (bad / code.name).write_bytes(bytes(blob))
        shutil.copy2(trial.parts / trial.manifest["assets"]["file"], bad / trial.manifest["assets"]["file"])
        corrupt_server = PartsServer(bad)
        trial.scenario("fail_corrupt_code", trial.stage, "uv", "plan", fast, switch=False, parts_base=corrupt_server.url)
        trial.scenario("fail_sync_bad_hash_uv", trial.stage, "uv", "plan", fast, switch=False, inject="sync-bad-hash")
        trial.scenario("fail_sync_bad_hash_pip", trial.stage, "pip", "plan", fast, switch=False, inject="sync-bad-hash")
        trial.scenario("fail_break_app", trial.stage, "uv", "plan", fast, switch=False, inject="break-app")
        trial.scenario("fail_break_lib", trial.stage, "uv", "plan", fast, switch=False, inject="break-lib")
    if want("big_jump"):
        for tool in ("uv", "pip"):
            trial.scenario(f"big_jump_{tool}_plan", trial.stage, tool, "plan", fast, switch=False, stop_after="sync", lock_version="4.5.0")
        trial.scenario("big_jump_uv_export", trial.stage, "uv", "export", fast, switch=False, stop_after="sync", lock_version="4.5.0")
    if args.os == "windows" and want("defender"):
        trial.scenario("defender_on", trial.defender, True)
        trial.scenario("defender_restart_app", lambda: {"stop": trial.stop_app(), "start": trial.start_app()})
        for method in clone_methods:
            trial.scenario(f"defender_clone_{method}", trial.clone_only, method)
        for tool in ("uv", "pip"):
            trial.scenario(f"defender_stage_{tool}_plan_{fast}", trial.stage, tool, "plan", fast, switch=True)
        trial.scenario("defender_stage_uv_plan_copy", trial.stage, "uv", "plan", "copy", switch=False, delete_after=True)
        trial.scenario("defender_status_end", trial.defender, False)
    if args.os == "linux" and want("linux"):
        trial.scenario("linux_launch_mode", linux_launch_mode, trial)
        label = trial.next_label()
        rep = trial.scenario(f"stage_for_systemd_{label}", trial.stage, "uv", "plan", fast, switch=False)
        if rep.get("ok"):
            trial.scenario("linux_systemd", linux_systemd, trial, rep.get("ready_version", rep["label"]))
    if want("rollback"):
        trial.scenario("rollback_rule", trial.rollback_rule)
    if want("cleanup"):
        final = trial.scenario("final_stage_and_switch", trial.stage, "uv", "plan", fast, switch=False)
        if final.get("ok"):
            trial.scenario("final_switch", trial.switch, final.get("ready_version", final["label"]))
            trial.scenario("cleanup_old_versions", trial.cleanup_old)
    trial.scenario("stop_all", trial.stop_app)
    trial.results["finished"] = iso()
    trial.save()
    return 0


if __name__ == "__main__":
    sys.exit(main())
