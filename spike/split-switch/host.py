"""Native launcher stand-in for Windows and Linux (Phase 1 spike, throwaway).

Plays the part PLAN.md gives to R\\Row-Bot.exe (Windows) and to the Linux
launch script: read state.json, apply the rollback rule, start
versions/<active> (Linux: releases/<active> via the `current` link), and
relaunch with the *same arguments* when the app asks for a restart.

The real 5.0.0/5.1.0 Python launcher knows nothing about state.json, so this
stand-in also does the Python launcher's step 3: it clears
pending_verification once the server answers /readyz.

Restart protocol used in the trial: the switcher sets "restart": true in
state.json before it stops the app (a real launcher would rather use an exit
code; see the handoff).

  python host.py --root R --layout windows|linux --port 8195 --log host.jsonl -- <launcher args>
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import engine  # noqa: E402

IS_WIN = os.name == "nt"


def log(path: Path, event: str, **fields: object) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"t": time.time(), "event": event, "host_pid": os.getpid(), **fields}, default=str) + "\n")


def ready(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/readyz", timeout=1) as response:  # noqa: S310 - loopback
            return response.status == 200
    except urllib.error.HTTPError:
        return False
    except Exception:  # noqa: BLE001
        return False


def processes_under(root: Path) -> list[dict]:
    import psutil

    root_s = str(root.resolve()).lower()
    out = []
    for proc in psutil.process_iter(["pid", "exe", "cmdline"]):
        try:
            exe = (proc.info.get("exe") or "").lower()
            cmd = " ".join(proc.info.get("cmdline") or []).lower()
        except Exception:  # noqa: BLE001
            continue
        if exe.startswith(root_s) or (root_s in cmd and "host.py" not in cmd and "driver.py" not in cmd):
            out.append({"pid": proc.info["pid"], "exe": proc.info.get("exe"), "cmd": cmd[:300]})
    return out


def resolve_active(root: Path, layout: str) -> tuple[dict, str]:
    state = engine.read_state(root)
    if layout == "linux":
        current = root / "current"
        link_version = Path(os.readlink(current)).name if current.is_symlink() else None
        if state is None:
            state = engine.new_state(link_version or "")
        elif link_version and state.get("active") != link_version:
            state["active_link_mismatch"] = link_version
    if state is None:
        versions = sorted(p.name for p in (root / "versions").iterdir() if p.is_dir() and not p.name.endswith(".staging"))
        state = engine.new_state(versions[-1])
    return state, state["active"]


def set_linux_current(root: Path, version: str) -> None:
    tmp = root / f".current.{os.getpid()}"
    if tmp.is_symlink():
        tmp.unlink()
    os.symlink(f"releases/{version}", tmp)
    os.replace(tmp, root / "current")


def start(root: Path, layout: str, version: str, args: list[str], data_dir: Path | None, logdir: Path) -> subprocess.Popen:
    if layout == "linux":
        vroot = root / "current"
        cmd = [str(vroot / "bin" / "row-bot"), *args]
        env = {k: v for k, v in os.environ.items() if not k.startswith(("PYTHON", "VIRTUAL_ENV"))}
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        if data_dir:
            env["ROW_BOT_DATA_DIR"] = str(data_dir)
        env.setdefault("BROWSER", "/bin/true")
        cwd = vroot / "app"
        flags = 0
    else:
        vroot = root / "versions" / version
        env = engine.runtime_env(vroot, data_dir)
        # python.exe (console) instead of pythonw.exe so the switcher can send
        # CTRL_BREAK to the launcher's own process group (graceful stop).
        cmd = [str(engine.Layout.python_exe(vroot)), str(vroot / "app" / "launcher.py"), *args]
        cwd = vroot / "app"
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    out = (logdir / f"launcher-{version}-{int(time.time() * 1000)}.log").open("ab")
    return subprocess.Popen(cmd, cwd=str(cwd), env=env, stdout=out, stderr=subprocess.STDOUT, creationflags=flags)  # noqa: S603


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--layout", choices=["windows", "linux"], required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--ready-timeout", type=float, default=240)
    parser.add_argument("--max-launches", type=int, default=6)
    parser.add_argument("--old-process-wait", type=float, default=30)
    parser.add_argument("launcher_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    launcher_args = [a for a in args.launcher_args if a != "--"]
    logdir = args.log.parent
    root = args.root
    log(args.log, "host_start", argv=sys.argv, launcher_args=launcher_args)
    launches = 0
    while launches < args.max_launches:
        state, _ = resolve_active(root, args.layout)
        state, rollback = engine.launch_decision(state)
        if rollback and args.layout == "linux":
            set_linux_current(root, state["active"])
        engine.write_state(root, state)
        if rollback:
            log(args.log, "rollback", reason=rollback, state=state)
        version = state["active"]
        launches += 1
        proc = start(root, args.layout, version, launcher_args, args.data_dir, logdir)
        log(args.log, "spawned", version=version, pid=proc.pid, launch=launches, failed_starts=state.get("failed_starts"),
            pending=state.get("pending_verification"))
        verified = False
        started = time.time()
        while proc.poll() is None:
            if not verified and ready(args.port):
                verified = True
                log(args.log, "ready", version=version, seconds=round(time.time() - started, 2))
                current = engine.read_state(root) or state
                if current.get("pending_verification") and current.get("active") == version:
                    current["pending_verification"] = False
                    current["failed_starts"] = 0
                    current["verified_at"] = engine.now_iso()
                    engine.write_state(root, current)
                    log(args.log, "verified", version=version)
            if not verified and time.time() - started > args.ready_timeout:
                log(args.log, "ready_timeout", version=version)
                break
            time.sleep(0.1)
        code = proc.poll()
        log(args.log, "launcher_exited", version=version, rc=code, verified=verified)
        # Nothing from the stopped version may survive the switch.
        vroot = root / ("releases" if args.layout == "linux" else "versions") / version
        deadline = time.time() + args.old_process_wait
        left = processes_under(vroot)
        while left and time.time() < deadline:
            time.sleep(0.2)
            left = processes_under(vroot)
        log(args.log, "old_processes_after_exit", version=version, left=left,
            waited=round(time.time() + args.old_process_wait - deadline, 2))
        state = engine.read_state(root) or state
        if state.pop("restart", False):
            engine.write_state(root, state)
            log(args.log, "restart_requested", next=state.get("active"))
            continue
        if state.get("pending_verification") and not verified:
            log(args.log, "failed_start", version=version, failed_starts=state.get("failed_starts"))
            continue
        log(args.log, "host_exit")
        return 0
    log(args.log, "host_exit_max_launches")
    return 1


if __name__ == "__main__":
    if IS_WIN:
        signal.signal(signal.SIGBREAK, signal.SIG_IGN)  # the switcher signals the launcher's group only
    sys.exit(main())
