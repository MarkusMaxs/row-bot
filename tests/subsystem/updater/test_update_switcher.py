"""Switching to a ready version: Restart now, on quit, the tray, and Repair."""

from __future__ import annotations

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
