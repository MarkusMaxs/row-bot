"""Velopack trial entry point (Phase 0 spike, throwaway, never shipped).

The trial payload installs this file as ``app/launcher.py`` and keeps the
real Row-Bot launcher as ``app/launcher_real.py``. The Windows stub
(``Row-Bot.exe``) and the macOS tray host both start ``launcher.py``.

Behaviour is driven by a one-shot control file so the CI driver can steer
runs that Setup.exe, Update.exe or UpdateMac start on their own:

    ~/rowbot-velopack-trial/control.json   (consumed on read)
    ~/rowbot-velopack-trial/trial.jsonl    (append-only event log)
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

T0 = time.time()
HOME = Path.home() / "rowbot-velopack-trial"
LOG = HOME / "trial.jsonl"
CONTROL = HOME / "control.json"
APP_DIR = Path(__file__).resolve().parent
IS_WINDOWS = sys.platform == "win32"


def log(event: str, **fields: object) -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    record = {"t": time.time(), "event": event, "pid": os.getpid(), **fields}
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, default=str) + "\n")


def installed_version() -> str | None:
    """Read <version> from Velopack's sq.version next to the main executable."""
    candidates = [APP_DIR.parent / "sq.version", APP_DIR.parents[1] / "MacOS" / "sq.version"]
    for path in candidates:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        start = text.find("<version>")
        end = text.find("</version>")
        if start >= 0 and end > start:
            return text[start + len("<version>") : end].strip()
    return None


def read_control() -> dict:
    try:
        data = json.loads(CONTROL.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"action": "idle"}
    except Exception as exc:  # noqa: BLE001
        log("control_unreadable", error=repr(exc))
        return {"action": "idle"}
    consumed = HOME / f"control.consumed.{int(time.time() * 1000)}.json"
    try:
        CONTROL.replace(consumed)
    except OSError as exc:
        log("control_consume_failed", error=repr(exc))
    return data


def bundled_python(gui: bool = False) -> str:
    if IS_WINDOWS:
        root = APP_DIR.parent / "python"
        return str(root / ("pythonw.exe" if gui else "python.exe"))
    return sys.executable


def wait_http(url: str, timeout: float) -> float | None:
    start = time.time()
    while time.time() - start < timeout:
        try:
            with urllib.request.urlopen(url, timeout=2):  # noqa: S310 - loopback only
                return time.time() - start
        except urllib.error.HTTPError:
            return time.time() - start  # the server answered
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.25)
    return None


def start_children(control: dict) -> list[dict]:
    """Start a real Row-Bot server child and a stand-in window process."""
    children: list[dict] = []
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    data_dir = HOME / "rowbot-data"
    data_dir.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["ROW_BOT_DATA_DIR"] = str(data_dir)
    env.pop("VELOPACK_RESTART", None)
    env.pop("VELOPACK_FIRSTRUN", None)
    port = int(control.get("server_port", 8195))
    if control.get("server_child", True):
        server_log = (HOME / f"server-{os.getpid()}.log").open("ab")
        proc = subprocess.Popen(  # noqa: S603 - trial only
            [
                bundled_python(),
                str(APP_DIR / "launcher_real.py"),
                "--server",
                "--no-open",
                "--no-splash",
                "--no-ollama",
                "--port",
                str(port),
            ],
            cwd=str(APP_DIR),
            env=env,
            stdout=server_log,
            stderr=server_log,
            creationflags=flags,
        )
        ready = wait_http(f"http://127.0.0.1:{port}/healthz", float(control.get("server_timeout", 240)))
        children.append({"kind": "server", "pid": proc.pid, "ready_seconds": ready, "port": port})
        log("server_child", pid=proc.pid, ready_seconds=ready, port=port)
    if control.get("window_child", True):
        proc = subprocess.Popen(  # noqa: S603 - trial only
            [bundled_python(gui=True), "-c", "import time; time.sleep(900)"],
            cwd=str(APP_DIR),
            env=env,
            creationflags=flags,
        )
        children.append({"kind": "window", "pid": proc.pid})
        log("window_child", pid=proc.pid)
    return children


def asset(entry: object) -> dict | None:
    if entry is None:
        return None
    return {
        "file": getattr(entry, "FileName", None),
        "version": getattr(entry, "Version", None),
        "type": getattr(entry, "Type", None),
        "size": getattr(entry, "Size", None),
    }


def do_update(velopack: object, control: dict) -> int:
    manager = velopack.UpdateManager(control["feed"])
    log(
        "manager",
        current=manager.get_current_version(),
        app_id=manager.get_app_id(),
        portable=manager.get_is_portable(),
        pending=asset(manager.get_update_pending_restart()),
    )
    started = time.time()
    info = manager.check_for_updates()
    log(
        "checked",
        seconds=time.time() - started,
        target=asset(info.TargetFullRelease) if info else None,
        base=asset(info.BaseRelease) if info else None,
        deltas=[asset(d) for d in info.DeltasToTarget] if info else [],
    )
    if info is None:
        log("no_update")
        return 0

    progress: list[tuple[float, int]] = []

    def on_progress(percent: int) -> None:
        if not progress or progress[-1][1] != percent:
            progress.append((round(time.time() - started, 3), int(percent)))

    started = time.time()
    try:
        manager.download_updates(info, on_progress)
    except Exception as exc:  # noqa: BLE001
        log("download_failed", seconds=time.time() - started, error=repr(exc))
        return 4
    log(
        "downloaded",
        seconds=time.time() - started,
        progress=progress[:: max(1, len(progress) // 25)],
        pending=asset(manager.get_update_pending_restart()),
    )
    if not control.get("apply", True):
        return 0

    children = start_children(control) if control.get("children") else []
    log("children", children=children)

    if control.get("quit_host") and not IS_WINDOWS:
        host = os.getppid()
        log("quit_host", host_pid=host)
        try:
            os.kill(host, signal.SIGTERM)
        except OSError as exc:
            log("quit_host_failed", error=repr(exc))

    if control.get("next"):
        CONTROL.write_text(json.dumps(control["next"]), encoding="utf-8")
    log("applying")
    manager.wait_exit_then_apply_updates(info, silent=True, restart=True, restart_args=[])
    log("exiting")
    os._exit(0)


def after_restart(control: dict) -> None:
    """Measure how quickly the updated payload serves (deterministic pyc check)."""
    if not control.get("start_server_after_restart"):
        return
    children = start_children({**control, "window_child": False})
    for child in children:
        try:
            os.kill(int(child["pid"]), signal.SIGTERM)
        except OSError:
            pass


def main() -> int:
    log(
        "start",
        argv=sys.argv,
        exe=sys.executable,
        cwd=os.getcwd(),
        ppid=os.getppid(),
        env_restart=os.environ.get("VELOPACK_RESTART"),
        env_firstrun=os.environ.get("VELOPACK_FIRSTRUN"),
        python=sys.version,
        app_dir=str(APP_DIR),
        installed_version=installed_version(),
    )
    try:
        import velopack
    except Exception as exc:  # noqa: BLE001
        log("import_failed", error=repr(exc))
        return 3
    log("imported", seconds=time.time() - T0, module=getattr(velopack, "__file__", None))

    app = velopack.App()
    # Velopack reads the *process* arguments by default, which for
    # "pythonw.exe launcher.py --veloapp-install X" starts with launcher.py.
    app.set_args(sys.argv[1:])
    app.on_first_run(lambda v: log("hook_first_run", version=v))
    app.on_restarted(lambda v: log("hook_restarted", version=v))
    if IS_WINDOWS:
        app.on_after_install_fast_callback(lambda v: log("hook_after_install", version=v))
        app.on_after_update_fast_callback(lambda v: log("hook_after_update", version=v))
        app.on_before_update_fast_callback(lambda v: log("hook_before_update", version=v))
        app.on_before_uninstall_fast_callback(lambda v: log("hook_before_uninstall", version=v))
    run_started = time.time()
    app.run()
    log("after_run", seconds=time.time() - run_started)

    control = read_control()
    log("control", **control)
    after_restart(control)
    if control.get("action") == "update":
        return do_update(velopack, control)
    time.sleep(float(control.get("idle_seconds", 1)))
    log("exit_idle")
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except BaseException as exc:  # noqa: BLE001
        log("crashed", error=repr(exc))
        raise
    sys.exit(code)
