"""The Stop bar's helper: the person's input told from Row-Bot's, Esc, and its line protocol."""

from __future__ import annotations

import io
import json
import sys
import time

import pytest

from row_bot.computer_use import control_bar
from row_bot.computer_use.control_bar import (
    ESC_VK,
    LLKHF_INJECTED,
    LLMHF_INJECTED,
    WM_KEYDOWN,
    WM_MOUSEMOVE,
    ControlBar,
    InputWatch,
)

pytestmark = [pytest.mark.subsystem, pytest.mark.platform]

WM_LBUTTONDOWN, WM_MOUSEWHEEL, WM_KEYUP = 0x0201, 0x020A, 0x0101


@pytest.fixture
def watch():
    events: list[dict] = []
    now = [0.0]
    made = InputWatch(events.append, clock=lambda: now[0])
    return made, events, now


def test_row_bots_own_input_never_counts_as_the_persons(watch):
    made, events, _now = watch
    for x in range(0, 400, 10):
        made.mouse(WM_MOUSEMOVE, x, x, injected=bool(LLMHF_INJECTED))
    made.mouse(WM_LBUTTONDOWN, 400, 400, injected=True)
    made.key(WM_KEYDOWN, 0x41, injected=bool(LLKHF_INJECTED))
    made.key(WM_KEYDOWN, ESC_VK, injected=True)  # Row-Bot pressing Esc in an app is not a Stop.

    assert events == []


def test_the_persons_mouse_and_keys_count_but_a_nudged_desk_doesnt(watch):
    made, events, now = watch
    made.mouse(WM_MOUSEMOVE, 100, 100, injected=False)
    made.mouse(WM_MOUSEMOVE, 103, 102, injected=False)
    assert events == []  # A few pixels: the desk was nudged.

    made.mouse(WM_MOUSEMOVE, 120, 110, injected=False)
    assert events == [{"event": "input", "kind": "mouse"}]
    made.mouse(WM_LBUTTONDOWN, 120, 110, injected=False)
    assert len(events) == 1  # Reported at most four times a second.

    now[0] += 1
    made.key(WM_KEYUP, 0x41, injected=False)
    made.key(WM_KEYDOWN, 0x41, injected=False)
    now[0] += 1
    made.mouse(WM_MOUSEWHEEL, 0, 0, injected=False)
    assert events[1:] == [{"event": "input", "kind": "key"}, {"event": "input", "kind": "mouse"}]


def test_the_persons_esc_stops_at_once(watch):
    made, events, _now = watch
    made.key(WM_KEYDOWN, ESC_VK, injected=False)
    made.key(WM_KEYDOWN, ESC_VK, injected=False)
    assert events == [{"event": "stop", "by": "esc"}] * 2  # Never throttled.


class _Process:
    def __init__(self, lines: list[str]) -> None:
        self.stdin = io.StringIO()
        self.stdout = io.StringIO("".join(line + "\n" for line in lines))
        self.pid = 4242
        self.waited = False

    def wait(self, timeout=None):
        self.waited = True
        return 0

    def kill(self):
        raise AssertionError("it quits when asked")


def test_the_bar_starts_once_hears_stop_and_input_and_quits_when_told():
    stops, inputs, launched = [], [], []
    process = _Process([json.dumps({"event": "ready", "pid": 4242}), "not json",
                        json.dumps({"event": "input", "kind": "key"}), json.dumps({"event": "stop", "by": "esc"})])
    sent: list[str] = []
    process.stdin.write = lambda text: sent.append(text) or len(text)

    def launch():
        launched.append(True)
        return process

    bar = ControlBar(on_stop=stops.append, on_input=inputs.append, launch=launch)
    bar.show("Row-Bot is using Microsoft Edge", (-2560, 40, 2400, 1300))
    bar.busy(True)
    bar.avoid((-1200.4, 900.6))
    deadline = time.monotonic() + 5
    while not stops and time.monotonic() < deadline:
        time.sleep(0.01)
    bar.close()

    assert launched == [True] and bar.pid == 4242
    assert [json.loads(text) for text in sent[:3]] == [
        {"words": "Row-Bot is using Microsoft Edge", "near": [-2560, 40, 2400, 1300]}, {"busy": True},
        {"avoid": [-1200, 901]}]
    assert json.loads(sent[-1]) == {"quit": True} and process.waited
    assert (stops, inputs) == (["esc"], ["key"])


def test_a_bar_that_cant_start_never_stops_computer_use():
    def launch():
        raise OSError("no display")

    bar = ControlBar(on_stop=lambda _by: None, on_input=lambda _kind: None, launch=launch)
    bar.show("Row-Bot is using Notepad")
    bar.close()
    assert bar.pid == 0


@pytest.mark.slow
@pytest.mark.skipif(sys.platform != "win32", reason="the real bar's window is checked on Windows")
def test_the_real_bar_shows_on_top_without_taking_focus_and_moves_off_a_click():
    import ctypes
    from ctypes import wintypes

    process = control_bar._launch_helper()
    try:
        process.stdin.write(json.dumps({"words": "Row-Bot is using Notepad", "near": None}) + "\n")
        process.stdin.flush()
        ready = json.loads(process.stdout.readline())
        assert ready["event"] == "ready"
        x0, y0, x1, y1 = ready["rect"]
        user32 = ctypes.windll.user32
        found = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def each(hwnd, _lparam):
            owner = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if owner.value == ready["pid"] and user32.IsWindowVisible(hwnd):
                found.append(hwnd)
            return True

        user32.EnumWindows(each, 0)
        [hwnd] = found
        style = user32.GetWindowLongW(hwnd, -20)
        assert style & 0x08000000 and style & 0x8  # Never activated; always on top.
        assert user32.GetForegroundWindow() != hwnd

        process.stdin.write(json.dumps({"avoid": [(x0 + x1) // 2, (y0 + y1) // 2]}) + "\n")
        process.stdin.flush()
        time.sleep(0.3)
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        assert rect.top < y0  # It moved to the top of the screen, off the point Row-Bot clicks.
    finally:
        process.stdin.write(json.dumps({"quit": True}) + "\n")
        process.stdin.flush()
        assert process.wait(timeout=10) == 0
