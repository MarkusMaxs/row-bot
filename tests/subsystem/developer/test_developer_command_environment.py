"""Developer Studio commands and processes start without Row-Bot's saved keys or its own secrets."""

from __future__ import annotations

import json
import pathlib
import subprocess

import pytest

pytestmark = [pytest.mark.subsystem, pytest.mark.platform]

SECRETS = {
    "OPENAI_API_KEY": "sk-external-synthetic",
    "DISCORD_BOT_TOKEN": "discord-external-synthetic",
    "ROW_BOT_LAUNCH_SECRET": "launch-secret-synthetic",
}
REPORT = 'python -c "import json, os; print(json.dumps(sorted(k.upper() for k in os.environ)))"'


@pytest.fixture
def runtime(tmp_path, monkeypatch, reload_for_data_dir):
    for name, value in SECRETS.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("WORKSPACE_FLAVOUR", "kept")
    _storage, _ledger, runtime = reload_for_data_dir(
        tmp_path / "data", "row_bot.developer.storage", "row_bot.developer.change_ledger",
        "row_bot.developer.runtime")
    return runtime


def _names(result) -> set[str]:
    assert result.ran is True and result.ok is True, result.stderr
    return set(json.loads(result.stdout))


def test_a_workspace_command_gets_no_keys(runtime, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()

    names = _names(runtime.run_workspace_command(str(repo), REPORT, "approve", confirmed=True))

    assert not set(SECRETS) & names
    assert "WORKSPACE_FLAVOUR" in names


@pytest.mark.slow  # Starts PowerShell on Windows; the command and process tests cover this in the PR lane.
def test_a_workspace_shell_command_gets_no_keys(runtime, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=str(repo), check=True, capture_output=True, text=True)

    names = _names(runtime.run_workspace_shell_command(
        str(repo), REPORT, "approve", workspace_id="", thread_id="thread-1", confirmed=True))

    assert not set(SECRETS) & names
    assert "WORKSPACE_FLAVOUR" in names


def test_a_long_running_process_starts_without_keys(runtime, tmp_path, monkeypatch):
    started: list[dict[str, str]] = []

    def popen(_argv, **kwargs):
        started.append(kwargs["env"])
        raise OSError("not started in this test")

    monkeypatch.setattr(runtime.subprocess, "Popen", popen)
    with pytest.raises(OSError):
        runtime.launch_tracked_process(pathlib.Path(tmp_path), ["npm", "run", "dev"], "npm run dev")

    assert not set(SECRETS) & {name.upper() for name in started[0]}
    assert started[0]["WORKSPACE_FLAVOUR"] == "kept"
