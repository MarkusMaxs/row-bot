"""The Linux bin/row-bot wrapper: environment, relaunch on 75, rollback, systemd.

Runs the real installer/linux/row-bot against fake releases whose
python/bin/python3 hands install_layout.py to this interpreter and otherwise
records the launch and exits with a scripted code.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from row_bot import install_layout as layout


pytestmark = [
    pytest.mark.subsystem,
    pytest.mark.installer,
    pytest.mark.platform,
    # Never on Windows: bash there can be WSL's, which ignores this environment.
    pytest.mark.skipif(os.name == "nt" or not shutil.which("bash"), reason="runs the POSIX wrapper"),
]

WRAPPER = Path("installer/linux/row-bot")
FAKE_PYTHON = """#!/usr/bin/env bash
if [ "${1:-}" = "-I" ]; then exec "$REAL_PYTHON" "$@"; fi
release="$(basename "$(dirname "$PWD")")"
printf '%s\\t%s\\t%s\\t%s\\t%s\\t%s\\t%s\\n' "$release" "$$" "${PYTHONDONTWRITEBYTECODE:-}" \\
    "${PYTHONNOUSERSITE:-}" "${ROW_BOT_INSTALL_ROOT:-}" "$PWD" "${*:2}" >> "$FAKE_RUNS"
codes="$PWD/exit_codes"
code=0
if [ -s "$codes" ]; then
    code="$(head -n 1 "$codes")"
    sed -i.bak '1d' "$codes"
fi
if [ "$code" = "sleep" ]; then
    trap 'echo stopped >> "$FAKE_RUNS"; exit 0' TERM
    while :; do sleep 0.1; done
fi
exit "$code"
"""


def _release(root: Path, version: str, *codes: str) -> Path:
    release = root / "releases" / version
    (release / "bin").mkdir(parents=True)
    (release / "python" / "bin").mkdir(parents=True)
    module = release / "app" / "src" / "row_bot" / "install_layout.py"
    module.parent.mkdir(parents=True)
    shutil.copy2(layout.__file__, module)
    (release / "app" / "launcher.py").write_text("# fake\n", encoding="utf-8")
    shutil.copy2(WRAPPER, release / "bin" / "row-bot")
    (release / "bin" / "row-bot").chmod(0o755)
    python = release / "python" / "bin" / "python3"
    python.write_text(FAKE_PYTHON, encoding="utf-8")
    python.chmod(0o755)
    (release / "app" / "exit_codes").write_text("".join(f"{code}\n" for code in codes), encoding="utf-8")
    return release


def _env(tmp_path: Path, **extra: str) -> dict[str, str]:
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path / "home"),
        "ROW_BOT_DATA_DIR": str(tmp_path / "data"),
        "REAL_PYTHON": sys.executable,
        "FAKE_RUNS": str(tmp_path / "runs.tsv"),
    }
    env.update(extra)
    return env


def _runs(tmp_path: Path) -> list[list[str]]:
    path = tmp_path / "runs.tsv"
    if not path.exists():
        return []
    return [line.split("\t") for line in path.read_text(encoding="utf-8").splitlines() if "\t" in line]


def _run(command: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, env=env, capture_output=True, text=True, timeout=60, check=False)


# Kept in the PR lane (under a second): the rollback guards against a broken update.
def test_two_failed_unverified_starts_roll_back_to_the_previous_release(tmp_path):
    root = tmp_path / "row-bot"
    _release(root, "1.0.0", "0")
    _release(root, "2.0.0", "3", "3", "3")
    layout.set_linux_current(root, "2.0.0")
    layout.write_state(root, layout.InstallState(active="2.0.0", previous="1.0.0", pending_verification=True))

    result = _run([str(root / "current" / "bin" / "row-bot"), "serve", "--port", "8123"], _env(tmp_path))

    assert result.returncode == 0, result.stderr
    assert [run[0] for run in _runs(tmp_path)] == ["2.0.0", "2.0.0", "1.0.0"]
    assert all(run[6] == "serve --port 8123" for run in _runs(tmp_path))
    assert os.readlink(root / "current") == "releases/1.0.0"
    state = layout.read_state(root)
    assert state.active == "1.0.0" and state.pending_verification is False
    assert state.last_rollback["from"] == "2.0.0"


@pytest.mark.slow
def test_the_wrapper_sets_the_launch_environment_and_keeps_the_arguments(tmp_path):
    root = tmp_path / "row-bot"
    release = _release(root, "1.0.0", "75", "0")
    layout.set_linux_current(root, "1.0.0")
    bin_home = tmp_path / "home" / ".local" / "bin"
    bin_home.mkdir(parents=True)
    (bin_home / "row-bot").symlink_to(root / "current" / "bin" / "row-bot")

    result = _run([str(bin_home / "row-bot"), "serve", "--port", "8123"], _env(tmp_path))

    assert result.returncode == 0, result.stderr
    runs = _runs(tmp_path)
    # Exit code 75 starts the active release again with the same arguments.
    assert [run[0] for run in runs] == ["1.0.0", "1.0.0"]
    for run in runs:
        assert run[2:] == ["1", "1", str(root.resolve()), str((release / "app").resolve()), "serve --port 8123"]
    # A first launch of a 5.1-era install writes state.json from `current`.
    assert layout.read_state(root).active == "1.0.0"


@pytest.mark.slow
def test_without_arguments_the_wrapper_opens_the_browser_client(tmp_path):
    root = tmp_path / "row-bot"
    _release(root, "1.0.0", "0")
    layout.set_linux_current(root, "1.0.0")

    assert _run([str(root / "current" / "bin" / "row-bot")], _env(tmp_path)).returncode == 0
    assert _runs(tmp_path)[0][6] == "--browser --no-tray"


@pytest.mark.slow
def test_under_systemd_the_unit_restarts_the_app(tmp_path):
    root = tmp_path / "row-bot"
    _release(root, "1.0.0", "75", "0")
    layout.set_linux_current(root, "1.0.0")
    command = [str(root / "current" / "bin" / "row-bot"), "serve"]

    process = subprocess.Popen(command, env=_env(tmp_path, INVOCATION_ID="unit"), stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True)
    process.communicate(timeout=60)

    assert process.returncode == 75
    runs = _runs(tmp_path)
    assert len(runs) == 1
    assert runs[0][1] == str(process.pid)  # exec'd: systemd sees the app itself


@pytest.mark.slow
def test_an_unpacked_tarball_runs_itself_without_state(tmp_path):
    package = tmp_path / "Row-Bot-1.0.0-Linux-x86_64"
    _release(tmp_path / "unpacked", "x", "0").rename(package)

    result = _run([str(package / "bin" / "row-bot"), "--server"], _env(tmp_path))

    assert result.returncode == 0, result.stderr
    assert _runs(tmp_path)[0][4] == str(package.resolve())
    assert not (tmp_path / "state.json").exists()


@pytest.mark.slow
def test_stopping_the_wrapper_stops_the_app_and_never_relaunches(tmp_path):
    root = tmp_path / "row-bot"
    _release(root, "1.0.0", "sleep", "0")
    layout.set_linux_current(root, "1.0.0")
    process = subprocess.Popen([str(root / "current" / "bin" / "row-bot"), "serve"], env=_env(tmp_path))
    deadline = time.monotonic() + 30
    while not _runs(tmp_path) and time.monotonic() < deadline:
        time.sleep(0.05)

    process.send_signal(signal.SIGTERM)

    assert process.wait(timeout=30) == 0
    assert (tmp_path / "runs.tsv").read_text(encoding="utf-8").splitlines()[-1] == "stopped"
    assert len(_runs(tmp_path)) == 1


@pytest.mark.slow
@pytest.mark.skipif(sys.platform != "linux", reason="install.sh uses GNU sed")
def test_install_sh_records_the_release_and_keeps_the_one_it_replaces(tmp_path):
    xdg = tmp_path / "xdg"
    home = tmp_path / "home"
    home.mkdir()
    env = {**_env(tmp_path), "XDG_DATA_HOME": str(xdg), "ROW_BOT_SUPPRESS_INSTALL_PATH_HINT": "1"}
    for version in ("1.0.0", "1.1.0", "1.2.0"):
        package = tmp_path / f"package-{version}"
        _release(tmp_path / "packages", version).rename(package)
        shutil.copy2("installer/linux/install.sh", package / "install.sh")
        (package / "install.sh").chmod(0o755)
        (package / "share" / "applications").mkdir(parents=True)
        (package / "share" / "applications" / "ai.row-bot.RowBot.desktop").write_text("Exec=row-bot\n", encoding="utf-8")
        (package / "install_info.json").write_text(json.dumps({"version": version}), encoding="utf-8")
        python = package / "python" / "bin" / "python3"
        python.write_text(f'#!/usr/bin/env bash\nexec "{sys.executable}" "$@"\n', encoding="utf-8")

        result = _run(["bash", str(package / "install.sh")], env)
        assert result.returncode == 0, result.stderr

    root = xdg / "row-bot"
    assert layout.read_state(root) == layout.InstallState(active="1.2.0", previous="1.1.0")
    assert os.readlink(root / "current") == "releases/1.2.0"
    assert sorted(path.name for path in (root / "releases").iterdir()) == ["1.1.0", "1.2.0"]
    assert os.readlink(home / ".local" / "bin" / "row-bot") == str(root / "current" / "bin" / "row-bot")
