"""Switching to a ready version: Restart now, on quit, the tray, and Repair."""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from row_bot import install_layout
from row_bot.updates import legacy, switcher
from tests.fixtures.updates import make_install, make_version

pytestmark = [pytest.mark.subsystem, pytest.mark.platform]


def _ready(tmp_path, version="5.2.1"):
    layout = make_install(tmp_path)
    make_version(layout.versions_dir, version, dists={"pip": "26.0"})
    state = install_layout.read_state(layout.root)
    state.ready = {"version": version, "staged_at": "2026-10-11T12:00:00Z"}
    install_layout.write_state(layout.root, state)
    return layout


def test_switching_needs_a_ready_version_that_can_start(tmp_path):
    layout = make_install(tmp_path)
    with pytest.raises(switcher.SwitchError):
        switcher.switch_to_ready(layout)
    state = install_layout.read_state(layout.root)
    state.ready = {"version": "5.2.1", "staged_at": "x"}
    install_layout.write_state(layout.root, state)
    # Named in state.json, but its folder is missing.
    assert switcher.ready_version(layout) is None
    with pytest.raises(switcher.SwitchError):
        switcher.switch_to_ready(layout)

    layout = _ready(tmp_path / "second")
    with pytest.raises(switcher.SwitchError):
        switcher.switch_to_ready(layout, expected="5.2.9")
    assert switcher.switch_to_ready(layout, expected="5.2.1") == "5.2.1"
    state = install_layout.read_state(layout.root)
    assert (state.active, state.previous, state.ready, state.pending_verification) == ("5.2.1", "5.2.0", None, True)
    assert "switched_at" in state.extra


def test_repair_goes_back_and_the_launcher_can_come_forward_again(tmp_path):
    layout = _ready(tmp_path)
    switcher.switch_to_ready(layout)
    running = install_layout.InstallLayout(layout.kind, layout.root, "5.2.1", layout.versions_dir / "5.2.1")
    assert switcher.rollback(running) == "5.2.0"
    state = install_layout.read_state(layout.root)
    assert (state.active, state.previous) == ("5.2.0", "5.2.1")
    assert state.last_rollback["reason"] == "repair"
    # Unknown state keys survive every write.
    assert "switched_at" in state.extra


def test_quitting_the_tray_applies_a_ready_update(tmp_path, monkeypatch):
    import row_bot.launcher as launcher

    layout = _ready(tmp_path)
    monkeypatch.setattr(install_layout, "detect", lambda **_kwargs: layout)
    exits, stopped = [], []
    tray = launcher.RowBotTray.__new__(launcher.RowBotTray)
    tray._quitting = False
    tray._stop_event = launcher.threading.Event()
    tray._window_proc = None
    tray._owns_server = True
    tray._server = SimpleNamespace(stop=lambda: stopped.append("server"))
    tray._launcher_control = None
    tray._icon = SimpleNamespace(stop=lambda: None)
    monkeypatch.setattr(launcher, "_clear_launcher_state", lambda: stopped.append("state"))
    monkeypatch.setattr(launcher.os, "_exit", exits.append)
    monkeypatch.setattr(launcher, "_QUIT_WATCHDOG_TIMEOUT", 60.0)

    tray._on_quit()
    for thread in list(launcher.threading.enumerate()):
        if thread.name == "quit-worker":
            thread.join(5)

    assert exits == [0]
    assert stopped == ["server", "state"]
    state = install_layout.read_state(layout.root)
    assert (state.active, state.previous, state.pending_verification) == ("5.2.1", "5.2.0", True)
    # The person quit: the next start is theirs, not a restart Row-Bot made.
    assert switcher.RESTART_REASON_KEY not in state.extra


def test_the_tray_shows_restart_to_update_only_while_one_is_ready(tmp_path, monkeypatch):
    import row_bot.launcher as launcher

    layout = _ready(tmp_path)
    monkeypatch.setattr(install_layout, "detect", lambda **_kwargs: layout)
    refreshed = []
    tray = launcher.RowBotTray.__new__(launcher.RowBotTray)
    tray._owns_server = True
    tray._update_ready = None
    tray._icon = SimpleNamespace(update_menu=lambda: refreshed.append(True))
    entries = {entry.label: entry for entry in launcher._build_tray_menu_entries(tray) if entry.label}
    item = entries["Restart to Update"]
    assert item.visible() is False

    tray._refresh_update_ready()
    assert item.visible() is True and refreshed == [True]
    tray._refresh_update_ready()
    assert refreshed == [True]  # unchanged: the menu is not rebuilt

    switcher.switch_to_ready(layout)
    tray._refresh_update_ready()
    assert item.visible() is False and refreshed == [True, True]


def test_the_tray_falls_back_to_switching_itself_when_the_app_cannot_answer(tmp_path, monkeypatch):
    import row_bot.launcher as launcher

    layout = _ready(tmp_path)
    monkeypatch.setattr(install_layout, "detect", lambda **_kwargs: layout)
    quits = []
    tray = launcher.RowBotTray.__new__(launcher.RowBotTray)
    tray._port = 1
    tray._server = SimpleNamespace(is_alive=False)
    tray._icon = SimpleNamespace(notify=lambda message: quits.append(message))
    tray._on_quit = lambda *, exit_code=0: quits.append(exit_code)

    tray._restart_to_update()

    assert quits == [install_layout.RELAUNCH_EXIT_CODE]
    assert install_layout.read_state(layout.root).active == "5.2.1"

    # When the app answers "deferred", the tray only says so.
    quits.clear()
    tray._server = SimpleNamespace(is_alive=True)
    monkeypatch.setattr(launcher, "_request_update_apply", lambda _port, _server: "deferred")
    tray._restart_to_update()
    assert len(quits) == 1 and "when its current work finishes" in quits[0]


def test_the_old_all_users_install_is_offered_only_when_recorded_and_present(tmp_path, monkeypatch):
    layout = make_install(tmp_path, kind=install_layout.InstallKind.WINDOWS)
    present = [True]
    monkeypatch.setattr(legacy, "legacy_install_present", lambda: present[0])
    assert legacy.pending(layout) is False
    state = install_layout.read_state(layout.root)
    state.extra[legacy.STATE_KEY] = {"detected_at": "2026-10-11T12:00:00Z"}
    install_layout.write_state(layout.root, state)
    assert legacy.pending(layout) is True
    present[0] = False
    assert legacy.pending(layout) is False
    legacy.clear(layout)
    assert legacy.STATE_KEY not in install_layout.read_state(layout.root).extra
    other = make_install(tmp_path / "linux", kind=install_layout.InstallKind.LINUX)
    assert legacy.pending(other) is False


def test_macos_repair_can_go_back_to_the_app_bundle(tmp_path):
    """The seed version lives in the bundle, outside versions/."""
    bundle = make_version(tmp_path / "Row-Bot.app" / "Contents", "Resources", dists={"pip": "26.0"})
    root = tmp_path / "Application Support" / "Row-Bot"
    staged = make_version(root / "versions", "5.2.1", dists={"pip": "26.0"})
    (staged / "python" / "bin").mkdir(parents=True, exist_ok=True)
    (staged / "python" / "bin" / "python3").write_bytes(b"python")
    install_layout.write_state(root, install_layout.InstallState(
        active="5.2.0", ready={"version": "5.2.1", "staged_at": "x"}))
    seed = install_layout.InstallLayout(install_layout.InstallKind.MACOS, root, "5.2.0", bundle, seed=True)

    assert switcher.switch_to_ready(seed) == "5.2.1"

    running = install_layout.InstallLayout(install_layout.InstallKind.MACOS, root, "5.2.1", staged)
    assert switcher.runnable_version(running, "5.2.0") is True
    assert switcher.runnable_version(running, "5.1.0") is False
    assert switcher.rollback(running) == "5.2.0"
    assert install_layout.read_state(root).active == "5.2.0"


def test_the_tray_never_bypasses_the_apps_restart_guard(tmp_path, monkeypatch):
    """Only a dead server (or nothing listening) lets the tray switch by itself."""
    import row_bot.launcher as launcher

    layout = _ready(tmp_path)
    monkeypatch.setattr(install_layout, "detect", lambda **_kwargs: layout)
    events = []
    tray = launcher.RowBotTray.__new__(launcher.RowBotTray)
    tray._port = 1
    tray._server = SimpleNamespace(is_alive=True)
    tray._icon = SimpleNamespace(notify=lambda message: events.append(message))
    tray._on_quit = lambda *, exit_code=0: events.append(exit_code)

    # The app answered with an error, a timeout or something unexpected.
    monkeypatch.setattr(launcher, "_request_update_apply", lambda _port, _server: "unavailable")
    tray._restart_to_update()
    assert len(events) == 1 and "can't restart to update right now" in events[0]
    assert install_layout.read_state(layout.root).active == "5.2.0"

    events.clear()
    monkeypatch.setattr(launcher, "_request_update_apply", lambda _port, _server: "unreachable")
    tray._restart_to_update()
    assert events == [install_layout.RELAUNCH_EXIT_CODE]
    state = install_layout.read_state(layout.root)
    assert state.active == "5.2.1"
    assert state.extra[switcher.RESTART_REASON_KEY]["reason"] == "update"  # the tray relaunches it


def test_asking_the_app_to_restart_tells_refused_from_unanswered(monkeypatch):
    import urllib.error

    import row_bot.launcher as launcher

    server = SimpleNamespace(_launch_environment={launcher.LAUNCH_SECRET_ENV: "s" * 40})
    sent = []

    class Answer:
        def __init__(self, body):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def read(self, _limit):
            return self.body

    def answering(outcome):
        class _Opener:
            def open(self, request, timeout):  # noqa: ARG002
                sent.append((request.full_url, request.get_header("Authorization")))
                if isinstance(outcome, BaseException):
                    raise outcome
                return Answer(outcome)

        return lambda *_handlers: _Opener()

    cases = [
        (b'{"status": "restarting"}', "restarting"),
        (b'{"status": "deferred", "work": ["x"]}', "deferred"),
        (b'{"status": "whatever"}', "unavailable"),
        (b"not json", "unavailable"),
        (urllib.error.URLError(ConnectionRefusedError()), "unreachable"),
        (ConnectionRefusedError(), "unreachable"),
        (urllib.error.URLError(TimeoutError()), "unavailable"),
        (TimeoutError(), "unavailable"),
        (urllib.error.HTTPError("http://127.0.0.1:1/", 503, "busy", {}, None), "unavailable"),
    ]
    for outcome, expected in cases:
        monkeypatch.setattr(launcher.urllib.request, "build_opener", answering(outcome))
        assert launcher._request_update_apply(8080, server) == expected, outcome
    assert {url for url, _auth in sent} == {"http://127.0.0.1:8080/api/launcher-apply-update"}
    assert {auth for _url, auth in sent} == {"Bearer " + "s" * 40}


def test_external_mode_leaves_a_ready_version_to_the_host(tmp_path, monkeypatch):
    import row_bot.launcher as launcher

    layout = _ready(tmp_path)
    monkeypatch.setattr(install_layout, "detect", lambda **_kwargs: layout)
    monkeypatch.setenv("ROW_BOT_UPDATES", "external")
    assert launcher._ready_update_version() is None
    assert launcher._switch_to_ready_update() is False
    assert switcher.apply_on_quit(layout) is None
    assert switcher.switch_to_ready_locked(layout) is None
    assert install_layout.read_state(layout.root).active == "5.2.0"
    monkeypatch.delenv("ROW_BOT_UPDATES")
    assert launcher._ready_update_version() == "5.2.1"


def test_switching_waits_for_another_row_bot_preparing_the_install(tmp_path):
    layout = _ready(tmp_path)
    other = switcher.InstallLock.for_install(layout.root)
    assert other.acquire()
    try:
        assert switcher.apply_on_quit(layout) is None
        assert install_layout.read_state(layout.root).active == "5.2.0"
    finally:
        other.release()
    assert switcher.apply_on_quit(layout) == "5.2.1"


class _Key:
    def __init__(self, values):
        self.values = values

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def _fake_winreg(entries: dict, opened: list):
    """A stand-in ``winreg``: *entries* maps (hive, view) to the key's values."""
    module = SimpleNamespace(
        HKEY_LOCAL_MACHINE="HKLM", HKEY_CURRENT_USER="HKCU", KEY_READ=1, KEY_WOW64_64KEY=0x100,
        KEY_WOW64_32KEY=0x200,
    )

    def open_key(hive, path, _reserved, access):
        view = "64" if access & 0x100 else "32"
        opened.append((hive, view))
        values = entries.get((hive, view))
        if values is None or path != legacy.LEGACY_UNINSTALL_KEY:
            raise OSError("no such key")
        return _Key(values)

    def query_value(key, name):
        if name not in key.values:
            raise OSError("no such value")
        return key.values[name], 1

    module.OpenKey = open_key
    module.QueryValueEx = query_value
    return module


def _legacy_recorded(tmp_path):
    layout = make_install(tmp_path, kind=install_layout.InstallKind.WINDOWS)
    state = install_layout.read_state(layout.root)
    state.extra[legacy.STATE_KEY] = {"detected_at": "2026-10-11T12:00:00Z"}
    install_layout.write_state(layout.root, state)
    return layout


def test_removing_the_old_install_runs_its_own_uninstaller_with_fixed_arguments(tmp_path, monkeypatch):
    layout = _legacy_recorded(tmp_path)
    folder = tmp_path / "Program Files" / "Row-Bot"
    folder.mkdir(parents=True)
    uninstaller = folder / "unins000.exe"
    uninstaller.write_bytes(b"MZ")
    opened, started = [], []
    entry = {"UninstallString": f'"{uninstaller}" /SILENT /LOG="evil"', "InstallLocation": str(folder)}
    monkeypatch.setitem(sys.modules, "winreg", _fake_winreg({("HKLM", "32"): entry}, opened))
    monkeypatch.setattr(legacy, "_is_windows", lambda: True)
    monkeypatch.setattr(legacy, "_shell_execute", lambda program, arguments: started.append((program, arguments)) or 42)

    assert legacy.start_removal(layout) is True
    # The quoted path (with a space) is the program; the registry's arguments are not used.
    assert started == [(str(uninstaller), "/VERYSILENT /SUPPRESSMSGBOXES /NORESTART")]
    assert {hive for hive, _view in opened} == {"HKLM"}

    # Uninstaller gone but the program still there: nothing is run or deleted.
    uninstaller.unlink()
    started.clear()
    assert legacy.start_removal(layout) is False
    assert started == []

    # Both gone: only the leftover entry is deleted, in the view that holds it.
    folder.rmdir()
    assert legacy.start_removal(layout) is True
    program, arguments = started[0]
    assert program.lower().endswith("reg.exe")
    assert arguments == f'delete "HKLM\\{legacy.LEGACY_UNINSTALL_KEY}" /f /reg:32'


def test_an_old_install_known_only_to_the_current_user_is_never_removed(tmp_path, monkeypatch):
    layout = _legacy_recorded(tmp_path)
    started, opened = [], []
    entry = {"UninstallString": str(tmp_path / "unins000.exe")}
    (tmp_path / "unins000.exe").write_bytes(b"MZ")
    monkeypatch.setitem(sys.modules, "winreg", _fake_winreg({("HKCU", "64"): entry}, opened))
    monkeypatch.setattr(legacy, "_is_windows", lambda: True)
    monkeypatch.setattr(legacy, "_shell_execute", lambda program, arguments: started.append(program) or 42)

    assert legacy.legacy_install_present() is False
    assert legacy.start_removal(layout) is False
    assert started == [] and {hive for hive, _view in opened} == {"HKLM"}
    assert legacy.STATE_KEY not in install_layout.read_state(layout.root).extra  # nothing left to offer

    # Not Windows: the registry is never read.
    other = _legacy_recorded(tmp_path / "again")
    monkeypatch.setattr(legacy, "_is_windows", lambda: False)
    opened.clear()
    assert legacy.start_removal(other) is False
    assert opened == [] and started == []


def _running(layout, version="5.2.1"):
    return install_layout.InstallLayout(layout.kind, layout.root, version, layout.versions_dir / version)


def test_only_a_fresh_marker_for_the_running_version_counts(tmp_path):
    import calendar
    import time

    layout = _ready(tmp_path)
    assert switcher.apply_on_quit(layout) == "5.2.1"
    running = _running(layout)
    assert switcher.consume_restart_reason(running) is None  # quitting records nothing
    at = "2026-10-11T12:00:00Z"
    written = calendar.timegm(time.strptime(at, "%Y-%m-%dT%H:%M:%SZ"))

    def mark(version):
        state = install_layout.read_state(layout.root)
        state.extra[switcher.RESTART_REASON_KEY] = {"reason": "update", "version": version, "at": at}
        install_layout.write_state(layout.root, state)

    mark("5.2.1")
    assert switcher.consume_restart_reason(running, now=lambda: written + 5) == "update"
    assert switcher.RESTART_REASON_KEY not in install_layout.read_state(layout.root).extra
    mark("5.2.1")  # long ago: that restart didn't happen then
    assert switcher.consume_restart_reason(running, now=lambda: written + 3600) is None
    mark("5.2.9")  # the launcher went back from the version Row-Bot restarted into
    assert switcher.consume_restart_reason(running, now=lambda: written + 5) is None
    assert switcher.RESTART_REASON_KEY not in install_layout.read_state(layout.root).extra


def test_the_next_start_after_a_restart_row_bot_made_says_so_once(tmp_path, monkeypatch):
    from row_bot.application import startup_state

    layout = _ready(tmp_path)
    running = _running(layout)
    monkeypatch.setattr(install_layout, "detect", lambda **_kwargs: running)
    monkeypatch.setattr(startup_state, "_app_initiated", None)
    assert startup_state.last_exit_was_app_initiated() is False  # a plain start

    switcher.switch_to_ready(layout, restart_reason="update")  # Restart now, then exit 75
    monkeypatch.setattr(startup_state, "_app_initiated", None)  # the relaunched process
    assert startup_state.last_exit_was_app_initiated() is True
    assert startup_state.last_exit_was_app_initiated() is True  # every reader in this run agrees
    assert switcher.RESTART_REASON_KEY not in install_layout.read_state(layout.root).extra
    monkeypatch.setattr(startup_state, "_app_initiated", None)  # the launch after that
    assert startup_state.last_exit_was_app_initiated() is False
