"""The app window and the macOS host, as the launcher sees them (B382).

- A second start of Row-Bot (or the macOS app opened again) shows the window, or opens a
  new one when the old one has gone; Row-Bot in the browser opens the browser.
- The launcher quits, stopping the server and the window, when its macOS host has gone.
- Quitting Row-Bot from its window quits all of it (the host too); closing the window doesn't.
"""
from __future__ import annotations

import json
import os
import signal
import threading
from types import SimpleNamespace

import pytest

import row_bot.launcher as launcher

pytestmark = [pytest.mark.subsystem, pytest.mark.platform]


class _Window:
    def __init__(self, pid: int, *, exit_code: int | None = None) -> None:
        self.pid = pid
        self.exit_code = exit_code
        self._exited = threading.Event()
        if exit_code is not None:
            self._exited.set()

    def poll(self) -> int | None:
        return self.exit_code

    def wait(self) -> int | None:
        self._exited.wait(5)
        return self.exit_code

    def end(self, code: int) -> None:
        self.exit_code = code
        self._exited.set()


@pytest.fixture
def events(monkeypatch):
    monkeypatch.setattr(launcher, "_EXIT_REQUESTED", threading.Event())
    monkeypatch.setattr(launcher, "_APP_QUIT_REQUESTED", threading.Event())
    return launcher


def _run_direct_with_a_second_start(tmp_path, monkeypatch, *, windows, native=True, window_answers=False):
    """Run the launcher in direct mode; while it runs, a second start asks it to show Row-Bot."""
    opened, browsed, asked, terminated = [], [], [], []
    controls = []

    class FakeServer:
        def __init__(self, port, host=None):
            self._proc = SimpleNamespace(pid=1111)

        def start(self, port=None, host=None):
            pass

        def stop(self):
            pass

        @staticmethod
        def _terminate_process(proc, *, label, timeout, kill_tree):
            terminated.append(proc.pid)

        is_alive = True

    class FakeControl:
        port = 4567
        open_token = "token"

        def __init__(self, _restart, **options):
            controls.append(options)

        def start(self):
            pass

        def stop(self):
            pass

        def child_environment(self):
            return {}

    def second_start(server, owns_server, **_kwargs):
        assert controls[0]["open_window"]() is True
        for thread in threading.enumerate():
            if thread.name == "row-bot-open-again":
                thread.join(5)

    def open_window(port, control_port=None):
        opened.append(control_port)
        return windows.pop(0)

    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("ROW_BOT_HOST", raising=False)
    monkeypatch.delenv(launcher._NATIVE_HOST_ENV, raising=False)
    for name, value in {
        "_launch_event": lambda *_a, **_k: None,
        "_maybe_start_ollama": lambda **_k: None,
        "_select_app_port": lambda preferred: (8092, False),
        "_claim_early_splash": lambda: None,
        "_stop_launcher_helper": lambda *_a, **_k: None,
        "_wait_for_server": lambda *_a, **_k: True,
        "_verify_install_when_ready": lambda *_a, **_k: None,
        "_has_display_server": lambda: True,
        "_find_free_port": lambda start, max_tries=50: 18092,
        "_RowBotProcess": FakeServer,
        "LauncherControlServer": FakeControl,
        "_block_until_interrupted": second_start,
        "_open_window": open_window,
        "_open_in_browser": lambda port: browsed.append(port),
        "_ask_window": lambda port, path, **_k: asked.append((port, path)) or window_answers,
        "_apply_ready_update_on_quit": lambda: None,
    }.items():
        monkeypatch.setattr(launcher, name, value)
    args = SimpleNamespace(port=8092, host=None, no_ollama=True, no_splash=True, server=False, no_open=False,
                           native=native, browser=not native)

    launcher._run_direct(args)

    state = json.loads((tmp_path / "launcher_state.json").read_text(encoding="utf-8"))
    return SimpleNamespace(opened=opened, browsed=browsed, asked=asked, terminated=terminated, state=state)


def test_a_second_start_opens_a_new_window_when_the_old_one_has_gone(tmp_path, monkeypatch, events) -> None:
    gone, new = _Window(2222, exit_code=0), _Window(3333)

    run = _run_direct_with_a_second_start(tmp_path, monkeypatch, windows=[gone, new])

    assert run.opened == [18092, 18092]
    assert run.state["window_pid"] == 3333 and run.state["window_control_port"] == 18092
    assert run.state["control_port"] == 4567  # a later start still finds this launcher
    assert run.browsed == []
    assert run.terminated == [3333]  # quitting closes the window it opened


def test_a_second_start_shows_the_window_that_is_still_open(tmp_path, monkeypatch, events) -> None:
    run = _run_direct_with_a_second_start(tmp_path, monkeypatch, windows=[_Window(2222)], window_answers=True)

    assert run.asked == [(18092, "/main/show")]
    assert run.opened == [18092] and run.state["window_pid"] == 2222


def test_a_window_that_does_not_answer_is_replaced(tmp_path, monkeypatch, events) -> None:
    stuck, new = _Window(2222), _Window(3333)

    run = _run_direct_with_a_second_start(tmp_path, monkeypatch, windows=[stuck, new])

    assert run.terminated[0] == 2222 and run.state["window_pid"] == 3333


def test_a_second_start_opens_the_browser_for_row_bot_in_the_browser(tmp_path, monkeypatch, events) -> None:
    run = _run_direct_with_a_second_start(tmp_path, monkeypatch, windows=[], native=False)

    assert run.browsed == [8092, 8092]  # once at the start, once for the second start
    assert run.opened == []


def test_the_launcher_quits_when_its_macos_host_has_gone(monkeypatch, events) -> None:
    parents = iter([4321, 4321, 1])
    monkeypatch.setattr(launcher.os, "getppid", lambda: next(parents, 1))

    watcher = launcher._stop_when_host_quits(4321, interval=0.01)
    watcher.join(5)

    assert launcher._EXIT_REQUESTED.is_set()
    assert not launcher._APP_QUIT_REQUESTED.is_set()  # the host is gone: nothing to end


def test_only_the_macos_host_is_watched(monkeypatch) -> None:
    monkeypatch.delenv(launcher._NATIVE_HOST_ENV, raising=False)
    assert launcher._native_host_pid() is None
    assert launcher._stop_when_host_quits(None) is None


@pytest.mark.skipif(os.name != "posix", reason="the macOS host's launcher")
def test_the_macos_host_is_the_launchers_parent(monkeypatch) -> None:
    monkeypatch.setenv(launcher._NATIVE_HOST_ENV, "1")
    assert launcher._native_host_pid() == os.getppid()


def test_quitting_from_the_window_quits_row_bot_and_its_host(monkeypatch, events) -> None:
    killed = []
    monkeypatch.setattr(launcher.os, "getppid", lambda: 4321)
    monkeypatch.setattr(launcher.os, "kill", lambda pid, sig: killed.append((pid, sig)), raising=False)
    window = _Window(2222)

    launcher._quit_app_when_window_quits(window)
    window.end(launcher._WINDOW_QUIT_EXIT_CODE)
    assert launcher._EXIT_REQUESTED.wait(5)
    launcher._end_native_host(4321)

    assert launcher._APP_QUIT_REQUESTED.is_set()
    assert killed == [(4321, signal.SIGTERM)]


def test_closing_the_window_leaves_row_bot_running(monkeypatch, events) -> None:
    killed = []
    monkeypatch.setattr(launcher.os, "kill", lambda pid, sig: killed.append((pid, sig)), raising=False)
    window = _Window(2222)

    watcher = launcher._quit_app_when_window_quits(window)
    window.end(0)
    watcher.join(5)
    launcher._end_native_host(4321)

    assert not launcher._EXIT_REQUESTED.is_set()
    assert killed == []
