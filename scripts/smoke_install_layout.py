"""Smoke an installed Row-Bot's native launcher against the 5.2 layout contract.

Run after an installer smoke, on the real install (CI only). With the install's
own native launcher (Row-Bot.exe, the macOS tray host or the Linux row-bot
wrapper) it checks two things:

1. Relaunch: a stand-in version applies an "update" (state.json now names the
   installed version, pending verification) and exits with 75. The launcher
   must start the installed version with the same arguments; once its server
   answers /readyz, the Python launcher must clear pending_verification.
2. The cases marked "smoke" in contracts/install-layout/launch-cases.json,
   with A as the installed version and B as a stand-in that exits with the
   case's codes: a newer version that keeps failing to start must be rolled
   back to the installed one.

The stand-ins share the installed version's Python through a link and are
removed afterwards, and state.json is put back. Stdlib only.

    python scripts/smoke_install_layout.py --root R --kind windows --port 8099 -- \\
        R/Row-Bot.exe --server --no-open --no-splash --no-ollama --port 8099
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from smoke_app import _end_job, _job_owning_tree, _port_open  # noqa: E402

RELAUNCH_VERSION = "99.0.1-smoke"
STAND_IN_VERSION = "99.0.2-smoke"
CASES = Path(__file__).resolve().parents[1] / "contracts" / "install-layout" / "launch-cases.json"

RELAUNCH_LAUNCHER = """\
import json, os, sys
from pathlib import Path
root = Path(os.environ["ROW_BOT_INSTALL_ROOT"])
here = Path(__file__).resolve().parent
(here / "ran.json").write_text(json.dumps({
    "args": sys.argv[1:],
    "dont_write_bytecode": os.environ.get("PYTHONDONTWRITEBYTECODE"),
    "install_root": os.environ.get("ROW_BOT_INSTALL_ROOT"),
    "no_user_site": os.environ.get("PYTHONNOUSERSITE"),
}))
state = json.loads((root / "state.json").read_text(encoding="utf-8"))
# What an applied update does: the next version becomes active and unverified.
state.update(active=GOOD, previous=FAKE, pending_verification=True, unverified_launches=0, ready=None)
temporary = root / ".state.smoke.tmp"
temporary.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
os.replace(temporary, root / "state.json")
if LINUX:
    link = root / ".current.smoke"
    os.symlink("releases/" + GOOD, link)
    os.replace(link, root / "current")
sys.exit(75)
"""

STAND_IN_LAUNCHER = """\
import json, sys
from pathlib import Path
here = Path(__file__).resolve().parent
codes = json.loads((here / "codes.json").read_text(encoding="utf-8"))
code = codes.pop(0) if codes else 0
(here / "codes.json").write_text(json.dumps(codes), encoding="utf-8")
with (here / "runs.txt").open("a", encoding="utf-8") as runs:
    runs.write("run\\n")
sys.exit(code)
"""


def _versions_dir(root: Path, kind: str) -> Path:
    return root / ("releases" if kind == "linux" else "versions")


def _read_state(root: Path) -> dict:
    return json.loads((root / "state.json").read_text(encoding="utf-8"))


def _write_state(root: Path, state: dict) -> None:
    temporary = root / ".state.smoke.tmp"
    temporary.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, root / "state.json")


def _set_current(root: Path, version: str) -> None:
    link = root / ".current.smoke"
    if link.is_symlink():
        link.unlink()
    os.symlink(f"releases/{version}", link)
    os.replace(link, root / "current")


def _link_dir(link: Path, target: Path) -> None:
    if os.name == "nt":
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], check=True, capture_output=True)
    else:
        os.symlink(target, link, target_is_directory=True)


def _unlink_dir(link: Path) -> None:
    """Remove a junction or symlink without touching what it points at."""
    if os.name == "nt":
        if link.exists() or link.is_symlink():
            os.rmdir(link)
    elif link.is_symlink():
        link.unlink()


def _make_stand_in(root: Path, kind: str, version: str, good_root: Path, script: str) -> Path:
    folder = _versions_dir(root, kind) / version
    _remove_stand_in(folder)
    (folder / "app").mkdir(parents=True)
    _link_dir(folder / "python", good_root / "python")
    if kind == "linux":
        _link_dir(folder / "bin", good_root / "bin")
    (folder / "app" / "launcher.py").write_text(script, encoding="utf-8")
    return folder


def _remove_stand_in(folder: Path) -> None:
    for name in ("python", "bin"):
        _unlink_dir(folder / name)
    for name in ("python", "bin"):
        if (folder / name).exists():
            raise RuntimeError(f"refusing to delete {folder}: {name} is still linked")
    shutil.rmtree(folder, ignore_errors=True)


def _get(url: str) -> int:
    try:
        with urllib.request.urlopen(url, timeout=2) as response:  # noqa: S310 - loopback
            return int(response.status)
    except urllib.error.HTTPError as error:
        return int(error.code)
    except (OSError, urllib.error.URLError):
        return 0


class Launcher:
    """The native launcher under test, stopped with everything it started."""

    def __init__(self, command: list[str], data_dir: Path, log: Path) -> None:
        env = {**os.environ, "ROW_BOT_DATA_DIR": str(data_dir), "PYTHONIOENCODING": "utf-8"}
        self.log = log
        self._out = log.open("w", encoding="utf-8")
        self.proc = subprocess.Popen(  # noqa: S603 - the installed launcher
            command,
            env=env,
            stdout=self._out,
            stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            start_new_session=os.name != "nt",
        )
        self.job = _job_owning_tree(self.proc)

    def stop(self, port: int) -> None:
        if os.name == "nt":
            if self.job is not None:
                _end_job(self.job)
            elif self.proc.poll() is None:
                self.proc.kill()
        else:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and (self.proc.poll() is None or _port_open(port)):
                time.sleep(0.5)
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        try:
            self.proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and _port_open(port):
            time.sleep(0.5)
        self._out.close()


def _wait(description: str, check, timeout: float, launcher: Launcher) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return
        if launcher.proc.poll() is not None:
            raise AssertionError(f"the launcher exited ({launcher.proc.returncode}) before {description}")
        time.sleep(0.5)
    raise AssertionError(f"timed out after {timeout:.0f}s waiting for {description}")


def _state_matches(root: Path, **expected: object) -> bool:
    try:
        state = _read_state(root)
    except (OSError, ValueError):
        return False
    for key, value in expected.items():
        if key == "last_rollback" and isinstance(value, dict):
            if {field: (state.get(key) or {}).get(field) for field in value} != value:
                return False
        elif state.get(key) != value:
            return False
    return True


def _mapped(value, names: dict[str, str]):
    if isinstance(value, dict):
        return {key: _mapped(item, names) for key, item in value.items()}
    return names.get(value, value) if isinstance(value, str) else value


def run(root: Path, kind: str, port: int, command: list[str], timeout: float, *,
        seed: Path | None = None, seed_version: str | None = None) -> list[str]:
    """Both checks; *seed* is the macOS bundle's Resources folder (the seed version)."""
    results: list[str] = []
    original = _read_state(root) if (root / "state.json").exists() else None
    if kind == "linux":
        good = Path(os.readlink(root / "current")).name
    elif original is not None:
        good = original["active"]
    elif seed_version:
        good = seed_version
    else:
        raise AssertionError("no state.json and no --seed-version")
    good_root = _versions_dir(root, kind) / good
    if not good_root.is_dir() and seed is not None and good == seed_version:
        good_root = seed
    linux = kind == "linux"
    stand_ins = []
    work = Path(tempfile.mkdtemp(prefix="row_bot_layout_smoke_"))
    try:
        if _port_open(port):
            raise AssertionError(f"port {port} is already in use")

        # 1. Exit code 75 relaunches with the same arguments; ready clears pending.
        script = f"GOOD = {good!r}\nFAKE = {RELAUNCH_VERSION!r}\nLINUX = {linux!r}\n" + RELAUNCH_LAUNCHER
        relaunch = _make_stand_in(root, kind, RELAUNCH_VERSION, good_root, script)
        stand_ins.append(relaunch)
        _write_state(root, {"schema": 1, "active": RELAUNCH_VERSION, "previous": good, "ready": None,
                            "pending_verification": False, "unverified_launches": 0, "last_rollback": None})
        if linux:
            _set_current(root, RELAUNCH_VERSION)
        launcher = Launcher(command, work / "data-relaunch", work / "relaunch.log")
        try:
            _wait("/readyz after the relaunch", lambda: _get(f"http://127.0.0.1:{port}/readyz") == 200, timeout,
                  launcher)
            _wait("pending_verification to clear",
                  lambda: _state_matches(root, active=good, previous=RELAUNCH_VERSION, pending_verification=False,
                                         unverified_launches=0), 60, launcher)
        finally:
            launcher.stop(port)
        ran = json.loads((relaunch / "app" / "ran.json").read_text(encoding="utf-8"))
        assert ran["dont_write_bytecode"] == "1", ran
        assert ran["no_user_site"] == "1", ran
        assert Path(ran["install_root"]).resolve() == root.resolve(), ran
        assert str(port) in ran["args"], ran
        results.append(f"relaunch: {RELAUNCH_VERSION} exited 75, {good} started with {ran['args']} and was verified")

        # 2. The shared launch cases that end with the installed version serving.
        names = {"A": good, "B": STAND_IN_VERSION}
        cases = [case for case in json.loads(CASES.read_text(encoding="utf-8"))["cases"] if case.get("smoke")]
        for case in cases:
            assert set(case["runnable"]) == {"A", "B"} and case["launched"][-1] == "A", case["id"]
            stand_in = _make_stand_in(root, kind, STAND_IN_VERSION, good_root, STAND_IN_LAUNCHER)
            stand_ins.append(stand_in)
            (stand_in / "app" / "codes.json").write_text(json.dumps(case["exits"].get("B", [])), encoding="utf-8")
            state = {"schema": 1, "ready": None, "last_rollback": None, **_mapped(case["state"], names)}
            _write_state(root, state)
            if linux:
                _set_current(root, state["active"])
            launcher = Launcher(command, work / f"data-{case['id']}", work / f"{case['id']}.log")
            try:
                _wait(f"/readyz in {case['id']}", lambda: _get(f"http://127.0.0.1:{port}/readyz") == 200, timeout,
                      launcher)
                _wait(f"the final state of {case['id']}",
                      lambda: _state_matches(root, **_mapped(case["final"], names)), 60, launcher)
            finally:
                launcher.stop(port)
            runs_file = stand_in / "app" / "runs.txt"
            runs = runs_file.read_text(encoding="utf-8").split() if runs_file.exists() else []
            assert len(runs) == case["launched"].count("B"), (case["id"], runs)
            if linux:
                assert Path(os.readlink(root / "current")).name == good
            results.append(f"{case['id']}: {STAND_IN_VERSION} started {len(runs)} times, then {good} served; "
                           f"last_rollback={_read_state(root)['last_rollback']}")
            _remove_stand_in(stand_in)
            stand_ins.remove(stand_in)
    except BaseException:
        for log in sorted(work.glob("*.log")):
            print(f"--- {log.name} ---\n{log.read_text(encoding='utf-8', errors='replace')[-6000:]}")
        for data in sorted(work.glob("data-*")):
            for name in ("launcher-host.log", "launcher.log", "row_bot_app.log"):
                path = data / name
                if path.exists():
                    print(f"--- {data.name}/{name} ---\n{path.read_text(encoding='utf-8', errors='replace')[-6000:]}")
        raise
    finally:
        if original is not None:
            _write_state(root, original)
        else:
            (root / "state.json").unlink(missing_ok=True)
        if linux:
            _set_current(root, good)
        for folder in stand_ins:
            _remove_stand_in(folder)
        shutil.rmtree(work, ignore_errors=True)
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--root", type=Path, required=True, help="The install root holding state.json")
    parser.add_argument("--kind", choices=("windows", "macos", "linux"), required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--timeout", type=float, default=240.0)
    parser.add_argument("--seed", type=Path, help="macOS: the bundle's Contents/Resources folder")
    parser.add_argument("--seed-version", help="macOS: the bundle's version")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("give the native launcher command after --")
    for line in run(args.root, args.kind, args.port, command, args.timeout, seed=args.seed,
                    seed_version=args.seed_version):
        print(f"[PASS] {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
