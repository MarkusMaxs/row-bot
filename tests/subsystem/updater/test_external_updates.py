"""ROW_BOT_UPDATES=external: the host applies updates (decision D11).

Row-Bot runs no checks, downloads or staging, the agent's update tool is off,
Settings shows the host's status file, and Update now / Skip / Pause become
requests in the shared folder.
"""

from __future__ import annotations

import json
from uuid import uuid4

import pytest

from row_bot.updates import external

pytestmark = [pytest.mark.subsystem, pytest.mark.platform]


@pytest.fixture
def host(tmp_path, monkeypatch, reload_for_data_dir):
    folder = tmp_path / "host-updates"
    monkeypatch.setenv("ROW_BOT_UPDATES", "external")
    monkeypatch.setenv(external.UPDATES_DIR_ENV, str(folder))
    (updater,) = reload_for_data_dir(tmp_path / "data", "row_bot.updater")
    monkeypatch.setattr(updater, "_fetch_releases_payload", lambda _channel: pytest.fail("no checks in external mode"))
    from row_bot.application import client_updates

    client_updates._RECEIPTS.clear()
    return folder, updater, client_updates


def test_external_mode_runs_no_checks_downloads_or_scheduler(host, monkeypatch):
    _folder, updater, _api = host
    assert updater.update_mode() == "external"
    assert updater.check_for_updates(force=True) is None
    assert updater.get_engine() is None
    assert updater.start_background_update(explicit=True) is False
    monkeypatch.setattr(updater, "_scheduler_thread", None)
    updater.start_update_scheduler()
    assert updater._scheduler_thread is None


def test_the_agents_update_tool_is_off(host):
    from row_bot.tools import registry

    tool = registry.get_tool("row_bot_updater")
    assert tool.as_langchain_tools() == []


def test_settings_shows_the_hosts_status_and_commands_become_requests(host):
    folder, _updater, api = host
    folder.mkdir()
    (folder / "status.json").write_text(json.dumps({
        "schema": 1, "state": "ready", "current_version": "5.2.1", "available_version": "5.2.2",
        "message": "Ready to update", "notes_url": "https://github.com/example/row-bot/releases/tag/v5.2.2",
        "updated_at": "2026-10-11T12:00:00Z",
    }), encoding="utf-8")

    snapshot = api.read_updates()
    assert snapshot["mode"] == "external" and snapshot["dev_install"] is False
    assert snapshot["host"]["state"] == "ready"
    assert snapshot["host"]["available_version"] == "5.2.2"
    assert snapshot["engine"]["phase"] == "idle"

    receipt = api.execute_update_choice(
        owner_id="device", command_id=str(uuid4()), expected_revision=snapshot["revision"],
        action="apply", version="5.2.2", when="now",
    )
    assert receipt["status"] == "requested"
    request = json.loads((folder / "request.json").read_text(encoding="utf-8"))
    assert (request["schema"], request["action"], request["version"]) == (1, "update_now", "5.2.2")
    assert receipt["snapshot"]["host"]["request"]["action"] == "update_now"

    paused = api.execute_update_choice(
        owner_id="device", command_id=str(uuid4()), expected_revision=receipt["snapshot"]["revision"],
        action="pause",
    )
    assert paused["status"] == "requested"
    assert json.loads((folder / "request.json").read_text(encoding="utf-8"))["action"] == "pause"

    # The host acted on it: the request is no longer shown as waiting.
    request_id = json.loads((folder / "request.json").read_text(encoding="utf-8"))["id"]
    status = json.loads((folder / "status.json").read_text(encoding="utf-8"))
    (folder / "status.json").write_text(json.dumps({**status, "state": "paused", "handled_request": request_id}),
                                        encoding="utf-8")
    assert api.read_updates()["host"]["request"] is None

    with pytest.raises(Exception, match="update_unavailable"):
        api.execute_update_choice(
            owner_id="device", command_id=str(uuid4()), expected_revision=api.read_updates()["revision"],
            action="rollback",
        )


def test_a_missing_or_hostile_status_file_reads_as_unknown(host):
    folder, _updater, _api = host
    assert external.read_status(folder)["state"] == "unknown"
    folder.mkdir()
    (folder / "status.json").write_text("{not json", encoding="utf-8")
    assert external.read_status(folder)["state"] == "unknown"
    (folder / "status.json").write_text(json.dumps({
        "schema": 1, "state": "rm -rf", "current_version": "../../etc", "notes_url": "javascript:alert(1)",
        "message": "x" * 5000,
    }), encoding="utf-8")
    status = external.read_status(folder)
    assert status["state"] == "unknown"
    assert status["current_version"] is None
    assert status["notes_url"] is None
    assert len(status["message"]) == 300
    (folder / "status.json").write_bytes(b" " * (external.MAX_STATUS_BYTES + 1))
    assert external.read_status(folder)["state"] == "unknown"
    with pytest.raises(ValueError):
        external.write_request("install_anything", folder=folder)  # type: ignore[arg-type]
