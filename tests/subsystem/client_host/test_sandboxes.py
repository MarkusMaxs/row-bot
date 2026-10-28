"""Sandboxes (5.3.1 B4): a second Row-Bot on its own data folder, port and workspace, on made-up data.

Nothing of the real profile reaches one: its environment carries no keys, tokens or Row-Bot
settings of this one, its data folder and workspace are its own, and the real data folder is
private to it. The deterministic tests run a fake process; one slow test starts a real sandbox.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from row_bot import sandboxes
from tests.subsystem.client_protocol.test_protocol_application import _client, service  # noqa: F401
from tests.subsystem.client_protocol.test_protocol_security import bootstrap

pytestmark = [pytest.mark.subsystem, pytest.mark.platform]


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv(sandboxes.SANDBOX_ENV, raising=False)
    monkeypatch.delenv(sandboxes.SANDBOX_OF_ENV, raising=False)
    return tmp_path


class _Process:
    """A sandbox process that answers at once and ends when it's told to."""

    def __init__(self, *args, **kwargs):
        self.args, self.env, self.cwd = args[0], kwargs["env"], kwargs["cwd"]
        self.pid, self.ended = os.getpid(), False

    def poll(self):
        return 0 if self.ended else None

    def terminate(self):
        self.ended = True

    def wait(self, timeout=None):
        return 0


@pytest.fixture
def fake_process(home, monkeypatch):
    started: list[_Process] = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: started.append(_Process(*a, **k)) or started[-1])
    monkeypatch.setattr(sandboxes, "_ping", lambda _port, _secret: True)
    monkeypatch.setattr("row_bot.process_cancellation.kill_process_tree", lambda _process: True)
    yield started
    sandboxes._PROCESSES.clear()
    sandboxes._SECRETS.clear()


def test_a_sandbox_gets_none_of_this_row_bots_keys_settings_or_folders(home):
    sandbox = sandboxes.create("Demo shop", "shop")
    env = sandboxes.environment(sandbox, 51234, "x" * 43, base={
        "PATH": "/usr/bin", "OPENAI_API_KEY": "sk-real", "GITHUB_TOKEN": "ghp", "AWS_SECRET_ACCESS_KEY": "a",
        "MY_PASSWORD": "p", "ROW_BOT_LEARNING_PREVIEW": "1", "ROW_BOT_DATA_DIR": str(home / "data"),
        "ROW_BOT_SECRET_STORE_KEY": "k", "HOME": str(home)})

    folder = home / "data" / "sandboxes" / sandbox.sandbox_id
    assert env["PATH"] == "/usr/bin" and env["HOME"] == str(home)
    for gone in ("OPENAI_API_KEY", "GITHUB_TOKEN", "AWS_SECRET_ACCESS_KEY", "MY_PASSWORD", "ROW_BOT_LEARNING_PREVIEW",
                 "ROW_BOT_SECRET_STORE_KEY"):
        assert gone not in env, gone
    assert (env["ROW_BOT_DATA_DIR"], env["ROW_BOT_WORKSPACE"]) == (str(folder / "data"), str(folder / "workspace"))
    assert (env["ROW_BOT_HOST"], env["ROW_BOT_PORT"]) == ("127.0.0.1", "51234"), "this computer only"
    assert env["ROW_BOT_UPDATES"] == "external" and env["ROW_BOT_INSTALL_COUNT"] == "0"
    assert env[sandboxes.SANDBOX_ENV] == "1" and env[sandboxes.SANDBOX_OF_ENV] == str(home / "data")


def test_inside_a_sandbox_the_real_data_folder_is_private_and_the_workspace_is_its_own(home, monkeypatch):
    from row_bot.data_paths import default_workspace_root, is_private_data_path

    real = home / "real-profile"
    (real / "memory").mkdir(parents=True)
    assert not is_private_data_path(real / "memory")
    monkeypatch.setenv(sandboxes.SANDBOX_OF_ENV, str(real))
    monkeypatch.setenv("ROW_BOT_WORKSPACE", str(home / "box" / "workspace"))
    assert is_private_data_path(real / "memory"), "its file tools never read or change the real profile"
    assert default_workspace_root() == home / "box" / "workspace"


def test_a_sandbox_starts_from_its_pack_runs_stops_resets_and_goes(fake_process, home):
    from row_bot.sandbox_packs import FILES

    sandbox = sandboxes.create("", "shop")
    workspace = home / "data" / "sandboxes" / sandbox.sandbox_id / "workspace"
    assert sandbox.name == "A small shop, with its own web app"
    assert sorted(str(path.relative_to(workspace)).replace("\\", "/") for path in workspace.rglob("*")
                  if path.is_file()) == sorted(FILES["shop"])

    started = sandboxes.start(sandbox.sandbox_id)
    assert started.status == "starting" and started.port
    [process] = fake_process
    assert process.args == [sys.executable, "-m", "row_bot.app"] and process.cwd == str(workspace)
    running = sandboxes.wait_running(sandbox.sandbox_id, 10)
    assert running.status == "running" and running.url == f"http://127.0.0.1:{running.port}/app-v2/"
    assert sandboxes.running_ports() == {running.port}

    (workspace / "notes.txt").write_text("made in the sandbox", encoding="utf-8")
    assert sandboxes.reset(sandbox.sandbox_id).status == "stopped" and process.ended
    assert not (workspace / "notes.txt").exists() and (workspace / "app" / "index.html").is_file()
    assert sandboxes.running_ports() == set()

    sandboxes.delete(sandbox.sandbox_id)
    assert sandboxes.get(sandbox.sandbox_id) is None and not workspace.exists()
    with pytest.raises(sandboxes.SandboxError, match="no longer here"):
        sandboxes.start(sandbox.sandbox_id)


def test_at_most_five_sandboxes_and_only_known_packs(home):
    with pytest.raises(sandboxes.SandboxError, match="one of"):
        sandboxes.create("x", "bank-accounts")
    for index in range(sandboxes.MAX_SANDBOXES):
        sandboxes.create(f"Box {index}")
    with pytest.raises(sandboxes.SandboxError, match="At most 5"):
        sandboxes.create("One more")


def test_settings_list_create_start_and_delete_sandboxes_but_a_sandbox_starts_none(service, fake_process,  # noqa: F811
                                                                                  monkeypatch):
    with _client(service) as client:
        _, headers = bootstrap(client)
        page = client.get("/api/v1/row-bot-sandboxes", headers=headers)
        assert page.status_code == 200, page.text
        assert page.json()["available"] and page.json()["items"] == []
        assert [pack["pack"] for pack in page.json()["packs"]] == ["empty", "shop", "charity", "paperwork", "code"]
        created = client.post("/api/v1/row-bot-sandboxes", headers=headers, json={"name": "Demo", "pack": "code"})
        assert created.status_code == 200, created.text
        box = created.json()["sandbox_id"]
        started = client.post(f"/api/v1/row-bot-sandboxes/{box}/start", headers=headers)
        assert started.json()["status"] in {"starting", "running"}
        assert client.post(f"/api/v1/row-bot-sandboxes/{box}/explode", headers=headers).status_code == 404
        assert client.post("/api/v1/row-bot-sandboxes/box-00000000/start", headers=headers).status_code == 404
        gone = client.delete(f"/api/v1/row-bot-sandboxes/{box}", headers=headers)
        assert gone.status_code == 200 and gone.json()["items"] == []

        monkeypatch.setenv(sandboxes.SANDBOX_ENV, "1")
        inside = client.get("/api/v1/row-bot-sandboxes", headers=headers).json()
        assert inside["available"] is False and inside["items"] == []
        refused = client.post("/api/v1/row-bot-sandboxes", headers=headers, json={"pack": "empty"})
        assert (refused.status_code, refused.json()["code"]) == (409, "row_bot_sandbox_refused")


def test_a_sandboxs_windows_carry_its_mark(service, monkeypatch):  # noqa: F811
    with _client(service) as client:
        handshake, _headers = bootstrap(client)
        assert handshake["sandbox"] is False
    monkeypatch.setenv(sandboxes.SANDBOX_ENV, "1")
    with _client(service) as client:
        handshake, _headers = bootstrap(client)
        assert handshake["sandbox"] is True


def test_row_bot_starts_a_sandbox_only_after_asking_and_opens_it_signed_in(fake_process, monkeypatch):
    from row_bot.tools import approval_gate, browser_tool

    asked: list[dict] = []
    monkeypatch.setattr(approval_gate, "current_approval_mode", lambda: "approve")
    monkeypatch.setattr("langgraph.types.interrupt", lambda payload: asked.append(payload) or False)
    assert "didn't want a sandbox" in browser_tool.sandbox("create", name="Demo", pack="shop")
    assert asked[0]["label"] == "Start a sandbox" and sandboxes.list_sandboxes() == []

    monkeypatch.setattr("langgraph.types.interrupt", lambda payload: True)
    made = browser_tool.sandbox("create", name="Demo", pack="shop")
    assert "running at http://127.0.0.1:" in made and "Open it with action open" in made
    box = sandboxes.list_sandboxes()[0].sandbox_id
    opened: list[str] = []
    monkeypatch.setattr(sandboxes, "sign_in_url", lambda _box: "http://127.0.0.1:1/app-v2/#sign-in=t")
    monkeypatch.setattr(browser_tool, "browser_navigate", lambda url: opened.append(url) or "Opened.")
    assert browser_tool.sandbox("open", sandbox=box).startswith("Opened Demo in this chat's browser, signed in.")
    assert opened == ["http://127.0.0.1:1/app-v2/#sign-in=t"]
    monkeypatch.setenv(sandboxes.SANDBOX_ENV, "1")
    assert "starts no sandboxes" in browser_tool.sandbox("list")


@pytest.mark.slow
def test_a_real_sandbox_starts_signs_a_browser_in_and_stops(home):
    sandbox = sandboxes.create("Real", "empty")
    try:
        sandboxes.start(sandbox.sandbox_id)
        running = sandboxes.wait_running(sandbox.sandbox_id, sandboxes.START_SECONDS)
        log = Path(home / "data" / "sandboxes" / sandbox.sandbox_id / "sandbox.log")
        assert running.status == "running", log.read_text(encoding="utf-8", errors="replace")[-2000:]
        link = sandboxes.sign_in_url(sandbox.sandbox_id)
        assert link.startswith(f"http://127.0.0.1:{running.port}/app-v2/#sign-in=")
    finally:
        sandboxes.stop(sandbox.sandbox_id)
    assert sandboxes.get(sandbox.sandbox_id).status == "stopped"
