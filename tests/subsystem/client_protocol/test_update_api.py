"""Settings › Updates through the client API, with the engine faked.

Reads never check or download. Every command is revision-bound and
idempotent by command id. Restart now is refused while work runs unless the
person confirms; "when idle" and "on quit" are the other choices.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from row_bot import install_layout
from row_bot.application import client_updates
from row_bot.updates.engine import EngineError, EngineStatus
from tests.fixtures.updates import make_install, make_version
from tests.subsystem.client_protocol.test_protocol_security import bootstrap, client_app

pytestmark = [pytest.mark.subsystem, pytest.mark.platform]


class FakeEngine:
    def __init__(self) -> None:
        self.state = EngineStatus()
        self.calls: list[tuple] = []
        self.busy: list[str] = []

    def status(self) -> EngineStatus:
        return self.state

    def start(self, release) -> bool:
        self.calls.append(("start", release.version))
        self.state = EngineStatus("downloading", release.version, step="code", total=10, downloaded=3)
        return True

    def cancel(self) -> bool:
        self.calls.append(("cancel",))
        return self.state.phase in {"downloading", "staging"}

    def apply(self, *, when, force=False, expected=None):
        self.calls.append(("apply", when, force, expected))
        if when == "now" and self.busy and not force:
            raise EngineError("update_restart_busy", list(self.busy))
        self.state = EngineStatus("applying" if when == "now" else "ready", "99.0.0", apply_when=when)
        return self.state

    def rollback(self) -> str:
        self.calls.append(("rollback",))
        return "1.0.0"

    def offer_full_installer(self, release, reason) -> None:
        self.calls.append(("full", release.version))

    def acknowledge(self) -> None:
        return None


def _updater(tmp_path, monkeypatch, *, engine: FakeEngine | None = None):
    from row_bot import updater

    monkeypatch.setattr(updater, "_CONFIG_PATH", tmp_path / "update_config.json")
    monkeypatch.setattr(updater, "_state", None)
    monkeypatch.setattr(updater, "is_dev_install", lambda: False)
    monkeypatch.delenv("ROW_BOT_UPDATES", raising=False)
    fake = engine or FakeEngine()
    monkeypatch.setattr(updater, "get_engine", lambda: fake)
    layout = make_install(tmp_path, "1.0.0")
    monkeypatch.setattr(client_updates, "_layout", lambda: layout)
    monkeypatch.setattr(updater, "ready_update", lambda: fake.state.version if fake.state.phase == "ready" else None)
    from row_bot.updates import activity

    monkeypatch.setattr(activity, "DEFAULT_PROBES", (lambda: (fake.busy or [None])[0],))
    client_updates._RECEIPTS.clear()
    return updater, fake, layout


def _release(updater):
    return updater.UpdateInfo(
        version="99.0.0", channel="stable", published_at="2026-09-23T10:00:00Z",
        notes_md="Synthetic release notes", notes_summary="Synthetic",
        asset_name="Row-Bot-99.0.0-Windows-x64.exe",
        asset_url="https://github.com/example/fixture.exe", asset_size=12,
        sha256="a" * 64, html_url="https://github.com/example/releases/99.0.0",
        is_prerelease=False,
        update_manifest_url="https://github.com/example/row-bot-update-99.0.0.json",
        update_manifest_sha256="b" * 64, update_manifest_size=900,
    )


def _send(action, snapshot, **kwargs):
    return client_updates.execute_update_choice(
        owner_id="local", command_id=str(uuid4()), expected_revision=snapshot["revision"], action=action, **kwargs,
    )


def test_update_read_is_passive_and_check_failure_is_not_up_to_date(tmp_path, monkeypatch):
    updater, engine, _layout = _updater(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(updater, "check_for_updates", lambda *, force: calls.append(force))
    first = client_updates.read_updates()
    assert first["available"] is None
    assert first["mode"] == "self" and first["auto_check"] and first["auto_download"]
    assert first["engine"]["phase"] == "idle"
    assert calls == [] and engine.calls == []
    assert not updater._CONFIG_PATH.exists()
    command_id = str(uuid4())
    result = client_updates.execute_update_choice(
        owner_id="local", command_id=command_id, expected_revision=first["revision"], action="check",
    )
    assert result["status"] == "failed"
    assert calls == [True]
    assert client_updates.execute_update_choice(
        owner_id="local", command_id=command_id, expected_revision=first["revision"], action="check",
    ) == result
    assert calls == [True]
    with pytest.raises(Exception, match="update_command_conflict"):
        client_updates.execute_update_choice(
            owner_id="local", command_id=command_id, expected_revision=first["revision"], action="download",
        )


def test_update_command_is_unavailable_from_development_checkout(tmp_path, monkeypatch):
    updater, _engine, _layout = _updater(tmp_path, monkeypatch)
    monkeypatch.setattr(updater, "is_dev_install", lambda: True)
    first = client_updates.read_updates()
    assert first["dev_install"] is True and first["mode"] == "unavailable"
    for action in ("check", "download", "apply"):
        with pytest.raises(Exception, match="update_unavailable"):
            _send(action, first, when="now" if action == "apply" else None)


def test_update_skip_and_clear_are_revision_bound(tmp_path, monkeypatch):
    updater, _engine, _layout = _updater(tmp_path, monkeypatch)
    state = updater.get_update_state()
    state.available = _release(updater)
    # Skips left from early testing were never offered and are not shown (B261).
    state.skipped_versions = ["9.9.9", "9.99.0"]

    def check(*, force):
        state.last_success = "2026-09-30T12:00:00Z"
        state.available = None if "99.0.0" in state.skipped_versions else _release(updater)

    monkeypatch.setattr(updater, "check_for_updates", check)
    first = client_updates.read_updates()
    assert first["available"]["verified_manifest"] is True
    assert first["skipped_versions"] == []
    with pytest.raises(Exception, match="update_changed"):
        _send("skip", first, version="98.0.0")
    skipped = _send("skip", first, version="99.0.0")["snapshot"]
    assert skipped["available"] is None
    assert skipped["skipped_versions"] == ["99.0.0"]
    # "Show it again" brings the release back.
    cleared = _send("clear_skipped", skipped)["snapshot"]
    assert cleared["skipped_versions"] == []
    assert cleared["available"]["version"] == "99.0.0"


def test_settings_channel_save_refreshes_the_shared_updater_owner(tmp_path, monkeypatch):
    updater, _engine, _layout = _updater(tmp_path, monkeypatch)
    from row_bot.application.settings_commands import _write_json_setting

    assert client_updates.read_updates()["channel"] == "stable"
    _write_json_setting(tmp_path, "preferences", "updates.channel", "beta")
    assert updater.get_update_state().channel == "beta"
    assert client_updates.read_updates()["channel"] == "beta"


def test_preferences_download_cancel_and_progress(tmp_path, monkeypatch):
    updater, engine, _layout = _updater(tmp_path, monkeypatch)
    updater.get_update_state().available = _release(updater)
    first = client_updates.read_updates()
    with pytest.raises(Exception, match="invalid_update_command"):
        _send("set_auto_download", first)
    off = _send("set_auto_download", first, enabled=False)["snapshot"]
    assert off["auto_download"] is False
    assert updater.get_update_state().auto_download is False
    assert engine.calls == []

    started = _send("download", off, version="99.0.0")
    assert started["status"] == "completed"
    assert engine.calls == [("start", "99.0.0")]
    progress = client_updates.read_updates()
    assert progress["engine"]["phase"] == "downloading"
    assert (progress["engine"]["downloaded"], progress["engine"]["total"]) == (3, 10)
    # Progress alone does not change what a command is bound to.
    engine.state.downloaded = 7
    assert client_updates.read_updates()["revision"] == progress["revision"]
    assert _send("cancel", progress)["status"] == "completed"
    assert engine.calls[-1] == ("cancel",)

    turned_on = _send("set_auto_check", client_updates.read_updates(), enabled=False)["snapshot"]
    assert turned_on["auto_check"] is False


def test_restart_now_is_guarded_while_work_runs(tmp_path, monkeypatch):
    updater, engine, _layout = _updater(tmp_path, monkeypatch)
    engine.state = EngineStatus("ready", "99.0.0")
    engine.busy = ["An agent is working"]
    ready = client_updates.read_updates()
    assert ready["engine"]["phase"] == "ready"
    assert ready["active_work"] == ["An agent is working"]

    with pytest.raises(Exception, match="update_restart_busy"):
        _send("apply", ready, version="99.0.0", when="now")
    with pytest.raises(Exception, match="invalid_update_command"):
        _send("apply", ready, version="99.0.0")
    later = _send("apply", ready, version="99.0.0", when="when_idle")
    assert later["snapshot"]["engine"]["apply_when"] == "when_idle"
    forced = _send("apply", later["snapshot"], version="99.0.0", when="now", force=True)
    assert forced["status"] == "completed"
    assert engine.calls[-1] == ("apply", "now", True, "99.0.0")
    assert forced["snapshot"]["engine"]["phase"] == "applying"


def test_repair_goes_back_to_the_previous_version(tmp_path, monkeypatch):
    _updater_, engine, layout = _updater(tmp_path, monkeypatch)
    assert client_updates.read_updates()["previous_version"] is None
    make_version(layout.versions_dir, "0.9.0", dists={"pip": "26.0"})
    state = install_layout.read_state(layout.root)
    state.previous = "0.9.0"
    install_layout.write_state(layout.root, state)
    snapshot = client_updates.read_updates()
    assert snapshot["previous_version"] == "0.9.0"
    with pytest.raises(Exception, match="update_changed"):
        _send("rollback", snapshot, version="0.8.0")
    assert _send("rollback", snapshot, version="0.9.0")["status"] == "completed"
    assert engine.calls == [("rollback",)]


def test_the_tray_restarts_now_or_waits_for_running_work(tmp_path, monkeypatch):
    _updater_, engine, _layout = _updater(tmp_path, monkeypatch)
    engine.state = EngineStatus("ready", "99.0.0")
    assert client_updates.apply_for_launcher() == {"status": "restarting"}
    engine.state = EngineStatus("ready", "99.0.0")
    engine.busy = ["A workflow is running"]
    assert client_updates.apply_for_launcher() == {"status": "deferred", "work": ["A workflow is running"]}
    assert engine.calls[-1] == ("apply", "when_idle", False, None)


def test_update_api_serves_every_signed_in_device_and_requires_command_identity(tmp_path, monkeypatch):
    updater, _engine, _layout = _updater(tmp_path, monkeypatch)
    calls = []

    def fake_check(*, force):
        calls.append(force)
        updater.get_update_state().last_success = "2026-09-23T12:00:00Z"

    monkeypatch.setattr(updater, "check_for_updates", fake_check)
    local, _, _ = client_app()
    remote, _, active = client_app(remote=True)
    with local, remote:
        _, headers = bootstrap(local)
        _, remote_headers = bootstrap(remote)
        elsewhere = remote.get("/api/v1/system/updates", headers=remote_headers)
        assert elsewhere.status_code == 200, elsewhere.text
        first = local.get("/api/v1/system/updates", headers=headers)
        assert first.status_code == 200, first.text
        assert elsewhere.json()["revision"] == first.json()["revision"]
        assert first.json()["engine"]["phase"] == "idle"
        browser_get = local.get(
            "/api/v1/system/updates",
            headers={key: value for key, value in headers.items() if key != "Origin"},
        )
        assert browser_get.status_code == 200, browser_get.text
        assert local.get(
            "/api/v1/system/updates",
            headers={**headers, "Origin": "http://other.invalid"},
        ).status_code == 403
        assert calls == []
        command_id = str(uuid4())
        body = {"command_id": command_id, "expected_revision": first.json()["revision"],
                "action": "check", "version": ""}
        assert remote.post("/api/v1/system/updates/commands", headers=remote_headers, json=body).status_code == 409
        result = remote.post("/api/v1/system/updates/commands", headers={**remote_headers, "Idempotency-Key": command_id}, json=body)
        # Restart now from any device: refused while work runs.
        _engine.state = EngineStatus("ready", "99.0.0")
        _engine.busy = ["An agent is working"]
        ready = remote.get("/api/v1/system/updates", headers=remote_headers).json()
        apply_id = str(uuid4())
        busy = remote.post(
            "/api/v1/system/updates/commands",
            headers={**remote_headers, "Idempotency-Key": apply_id},
            json={"command_id": apply_id, "expected_revision": ready["revision"], "action": "apply",
                  "version": "99.0.0", "when": "now"},
        )
        active["value"] = False
        signed_out = remote.post("/api/v1/system/updates/commands",
                                 headers={**remote_headers, "Idempotency-Key": str(uuid4())}, json=body)
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "completed"
    assert calls == [True]
    assert busy.status_code == 409 and busy.json()["code"] == "update_restart_busy"
    assert signed_out.status_code == 401


def test_attention_says_restart_to_update_once_an_update_is_ready(tmp_path, monkeypatch):
    import sys

    updater, engine, _layout = _updater(tmp_path, monkeypatch)
    monkeypatch.setitem(sys.modules, "row_bot.updater", updater)
    from row_bot.application.client_monitor import read_attention

    updater.get_update_state().available = _release(updater)
    assert read_attention(include_update=True)["update"] == {"version": "99.0.0", "ready": False}
    engine.state = EngineStatus("ready", "99.0.0")
    assert read_attention(include_update=True)["update"] == {"version": "99.0.0", "ready": True}
    assert read_attention(include_update=False)["update"] is None


def test_a_command_stays_bound_while_the_step_its_message_and_checking_change(tmp_path, monkeypatch):
    updater, engine, _layout = _updater(tmp_path, monkeypatch)
    engine.state = EngineStatus("staging", "99.0.0", step="clone", message="Copying the runtime")
    first = client_updates.read_updates()
    engine.state = EngineStatus("staging", "99.0.0", step="libraries", message="Installing libraries",
                                downloaded=9, total=10)
    assert client_updates.read_updates()["revision"] == first["revision"]
    assert _send("cancel", first)["status"] == "completed"  # a second-old snapshot still works

    engine.state = EngineStatus()
    idle = client_updates.read_updates()
    monkeypatch.setattr(updater, "is_checking", lambda: True)
    checking = client_updates.read_updates()
    assert checking["engine"]["phase"] == "checking"
    assert checking["revision"] == idle["revision"]
    # A real change of phase still changes the revision.
    engine.state = EngineStatus("ready", "99.0.0")
    assert client_updates.read_updates()["revision"] != idle["revision"]


def test_settings_shows_a_held_back_version_and_a_test_feed(tmp_path, monkeypatch):
    updater, _engine, layout = _updater(tmp_path, monkeypatch)
    snapshot = client_updates.read_updates()
    assert snapshot["held_back"] is None and snapshot["feed_url"] is None
    state = install_layout.read_state(layout.root)
    state.last_rollback = {"from": "1.1.0", "to": "1.0.0", "at": "2026-10-11T12:00:00Z",
                           "reason": "2 launches in a row did not become ready"}
    install_layout.write_state(layout.root, state)
    monkeypatch.setenv(updater.UPDATE_FEED_ENV, "http://127.0.0.1:8765")
    monkeypatch.setenv(updater.UPDATE_FEED_TESTING_ENV, "1")
    snapshot = client_updates.read_updates()
    assert snapshot["held_back"] == {"version": "1.1.0", "reason": "rolled_back"}
    assert snapshot["feed_url"] == "http://127.0.0.1:8765/"
    state.last_rollback["reason"] = "repair"
    install_layout.write_state(layout.root, state)
    assert client_updates.read_updates()["held_back"]["reason"] == "repair"


def test_removing_the_old_install_is_requested_and_never_blocks_settings(tmp_path, monkeypatch):
    import threading

    from row_bot.updates import legacy

    _updater(tmp_path, monkeypatch)
    # The earlier all-users install exists only on Windows: a per-user Windows
    # install that recorded it, and its registry entry (faked) still present.
    monkeypatch.setattr(legacy, "legacy_install_present", lambda: True)
    for kind in (install_layout.InstallKind.LINUX, install_layout.InstallKind.MACOS):
        other = make_install(tmp_path / kind.value, "1.0.0", kind=kind)
        state = install_layout.read_state(other.root)
        state.extra[legacy.STATE_KEY] = {"detected_at": "2026-10-11T12:00:00Z"}
        install_layout.write_state(other.root, state)
        monkeypatch.setattr(client_updates, "_layout", lambda other=other: other)
        assert client_updates.read_updates()["legacy_install"] is False  # never offered off Windows
    layout = make_install(tmp_path / "windows", "1.0.0", kind=install_layout.InstallKind.WINDOWS)
    monkeypatch.setattr(client_updates, "_layout", lambda: layout)
    state = install_layout.read_state(layout.root)
    state.extra[legacy.STATE_KEY] = {"detected_at": "2026-10-11T12:00:00Z"}
    install_layout.write_state(layout.root, state)
    prompt_open = threading.Event()
    answered = threading.Event()

    def windows_prompt(_layout):
        prompt_open.set()
        answered.wait(10)  # Windows' elevation prompt waits for the person
        return True

    monkeypatch.setattr(legacy, "start_removal", windows_prompt)
    snapshot = client_updates.read_updates()
    assert snapshot["legacy_install"] is True
    try:
        assert _send("remove_legacy", snapshot)["status"] == "requested"
        assert prompt_open.wait(10)
        # While the prompt is open, Settings still reads and other commands run.
        assert client_updates.read_updates()["legacy_install"] is True
        assert _send("set_auto_check", client_updates.read_updates(), enabled=False)["status"] == "completed"
    finally:
        answered.set()


def test_the_launchers_restart_to_update_needs_its_launch_secret(monkeypatch):
    import importlib

    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    app_module = importlib.import_module("row_bot.app")
    secret = "s" * 40
    calls: list[bool] = []
    monkeypatch.setattr(client_updates, "apply_for_launcher", lambda: calls.append(True) or {"status": "restarting"})
    local = Starlette()
    local.add_route("/api/launcher-apply-update", app_module._launcher_apply_update_handler, methods=["POST"])
    client = TestClient(local)
    url = "/api/launcher-apply-update"

    monkeypatch.delenv(app_module.LAUNCH_SECRET_ENV, raising=False)
    assert client.post(url, headers={"Authorization": "Bearer "}).status_code == 403
    monkeypatch.setenv(app_module.LAUNCH_SECRET_ENV, secret)
    assert client.post(url).status_code == 403
    assert client.post(url, headers={"Authorization": "Bearer wrong"}).status_code == 403
    assert client.post(url, headers={"Authorization": f"Bearer {secret[:-1]}"}).status_code == 403
    assert calls == []
    answer = client.post(url, headers={"Authorization": f"Bearer {secret}"})
    assert answer.status_code == 200 and answer.json() == {"status": "restarting"}
    assert calls == [True]
