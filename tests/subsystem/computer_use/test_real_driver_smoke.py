"""The real Cua Driver on a fixture window of this test's own: a short task, end to end (Windows).

Opt-in and slow: set ``ROW_BOT_CUA_DRIVER`` to a reviewed ``cua-driver.exe``
(0.20.0). The test opens its own small window ("Smoke Notes": a magenta Count
button and a cyan field), finds them in the picture the model would get,
clicks Count and types into the field with real input through the
``computer`` tool, with the real Stop bar showing. It touches nothing else on
the desktop. Cua's telemetry is turned off for this child process.

It runs twice: on a DPI-unaware window (Windows stretches it on a scaled
display) and on one that draws at the display's scale. Before any input it
checks that the point Row-Bot aims at is on the button where the window
really put it on screen; every step prints the window's rectangle and DPI,
the aimed screen point, Cua's point and where the pointer went, and saves
the model's picture with the aim marked under the test's temporary folder.
"""

from __future__ import annotations

import io
import logging
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path

import pytest

pytestmark = [pytest.mark.subsystem, pytest.mark.platform, pytest.mark.slow]

DRIVER = os.environ.get("ROW_BOT_CUA_DRIVER", "")
FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "desktop_fixture_app.py"


def _find(picture: bytes, colour: tuple[int, int, int]) -> tuple[int, int]:
    """The middle of the patch of *colour* in a picture: what a model looking at it would aim at."""

    from PIL import Image

    with Image.open(io.BytesIO(picture)) as image:
        rgb = image.convert("RGB")
        points = [(x, y) for y in range(0, rgb.height, 2) for x in range(0, rgb.width, 2)
                  if all(abs(a - b) < 40 for a, b in zip(rgb.getpixel((x, y)), colour))]
    assert points, f"no {colour} in the picture"
    xs, ys = [x for x, _y in points], [y for _x, y in points]
    return (min(xs) + max(xs)) // 2, (min(ys) + max(ys)) // 2


def _screen_locked() -> bool:
    """A locked Windows session has no desktop to type into: the smoke can't run there."""

    import ctypes
    from ctypes import wintypes

    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    desktop = user32.OpenInputDesktop(0, False, 0x0100)  # DESKTOP_SWITCHDESKTOP
    if not desktop:
        return True
    user32.CloseDesktop(desktop)
    owner = wintypes.DWORD()
    user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), ctypes.byref(owner))
    process = kernel32.OpenProcess(0x1000, False, owner.value)  # PROCESS_QUERY_LIMITED_INFORMATION
    name = ctypes.create_unicode_buffer(512)
    size = wintypes.DWORD(512)
    kernel32.QueryFullProcessImageNameW(process, 0, name, ctypes.byref(size))
    kernel32.CloseHandle(process)
    return name.value.casefold().endswith(("\\lockapp.exe", "\\logonui.exe"))


def _aim(shot, colour: tuple[int, int, int], target: tuple[int, int, int, int], picture: Path):
    """Where Row-Bot aims for a control of *colour*, with what it knows; fails before any input if the aim
    misses where the window really put the control (*target*: left, top, right, bottom on screen)."""

    from PIL import Image, ImageDraw

    from row_bot.computer_use import display

    x, y = _find(shot.image, colour)
    aimed = shot.to_screen(x, y)
    facts = display.window_display(shot.window.window_id)
    with Image.open(io.BytesIO(shot.image)) as source:
        marked = source.convert("RGB")
    draw = ImageDraw.Draw(marked)
    draw.ellipse((x - 9, y - 9, x + 9, y + 9), outline=(255, 0, 0), width=3)
    marked.save(picture)
    report = (f"window {shot.window.bounds} ({facts.words() if facts else 'no display facts'}); "
              f"Cua's picture {shot.cua_size}, the model's {shot.size}; aim {x},{y} → screen "
              f"({aimed[0]:.1f}, {aimed[1]:.1f}) → Cua {shot.to_cua(x, y)}; the control is at {target} on screen; "
              f"picture with the aim: {picture}")
    print(report)
    left, top, right, bottom = target
    assert left <= aimed[0] < right and top <= aimed[1] < bottom, f"The aim misses the control: {report}"
    return x, y, report


@pytest.mark.skipif(sys.platform != "win32" or not Path(DRIVER).is_file(),
                    reason="set ROW_BOT_CUA_DRIVER to a cua-driver.exe on Windows")
@pytest.mark.parametrize("aware", [False, True], ids=["dpi-unaware-window", "per-monitor-aware-window"])
def test_a_short_task_on_a_real_window_with_real_input(tmp_path, monkeypatch, caplog, aware):
    if _screen_locked():
        pytest.skip("the screen is locked: real input can't reach a window")
    from row_bot import agent
    from row_bot.browser import model_view
    from row_bot.computer_use import display
    from row_bot.computer_use.client import CuaClient
    from row_bot.computer_use.readiness import CuaReadiness, ReadinessCode, acknowledge_disclosure
    from row_bot.computer_use.service import ComputerUseService
    from row_bot.mcp_client.runtime import PrivateMcpSession
    from row_bot.tools import approval_gate, computer_use_tool

    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / "data"))
    acknowledge_disclosure()
    monkeypatch.setattr("row_bot.computer_use.readiness.readiness",
                        lambda **_kw: CuaReadiness(ReadinessCode.READY, "ready", executable=DRIVER))
    monkeypatch.setattr(agent, "get_active_runtime_context",
                        lambda: {"thread_id": "smoke", "generation_id": "smoke-turn", "runtime_surface": "normal_chat"})
    monkeypatch.setattr(approval_gate, "current_approval_mode", lambda: "full")
    monkeypatch.setattr(model_view, "model_sees_images", lambda model=None: True)
    # Someone at the computer moving the mouse: carry on (the test's window is the only one it acts on).
    monkeypatch.setattr("langgraph.types.interrupt",
                        lambda _card: {"row_bot_answer": "answered", "answers": [{"choices": [0], "other": ""}]})

    def quiet(executable: str, _session: str, env: dict[str, str]) -> PrivateMcpSession:
        return PrivateMcpSession(command=executable, args=["mcp"],
                                 env={**env, "CUA_DRIVER_RS_TELEMETRY_ENABLED": "0"}, timeout=60.0)

    stopped: list[str] = []
    service = ComputerUseService(client_factory=lambda: CuaClient(DRIVER, transport_factory=quiet),
                                 stop_turn=stopped.append)
    monkeypatch.setattr(computer_use_tool, "get_computer_use_service", lambda: service)

    caplog.set_level(logging.DEBUG, logger="row_bot.computer_use.service")
    app = subprocess.Popen([sys.executable, "-I", str(FIXTURE), *(["--aware"] if aware else [])],
                           stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, encoding="utf-8")
    lines: queue.Queue[str] = queue.Queue()
    threading.Thread(target=lambda: [lines.put(line.strip()) for line in app.stdout], daemon=True).start()
    on_screen: dict[str, tuple[int, int, int, int]] = {}

    def wait_for(text: str, why: str = "") -> None:
        while True:
            try:
                line = lines.get(timeout=15)
            except queue.Empty:
                raise AssertionError(f'The fixture never printed "{text}". {why}') from None
            name, _, rest = line.partition(" ")
            if name in {"button", "field"}:
                left, top, right, bottom = (int(value) for value in rest.split())
                on_screen[name] = (left, top, right, bottom)
            if line == text:
                return

    def pointer_words(result: str) -> str:
        return f"Row-Bot: {result.splitlines()[0]!r}; the pointer is at {display.cursor_position()}"

    try:
        wait_for("ready")
        listed = computer_use_tool.computer("list_apps")
        assert '"Smoke Notes"' in listed

        shown = computer_use_tool.computer("screenshot", app="Smoke Notes")
        assert shown.startswith("Took a screenshot.\n"), shown
        x, y, report = _aim(service.latest(), (255, 0, 255), on_screen["button"], tmp_path / "aim-count.png")
        clicked = computer_use_tool.computer("click", x=x, y=y)
        after = pointer_words(clicked)
        print(after)
        assert clicked.startswith(f"Clicked at {x},{y}"), f"{after}\n{report}"
        wait_for("clicked 1", f"The click didn't reach Count. {after}. {report}")

        x, y, report = _aim(service.latest(), (0, 255, 255), on_screen["field"], tmp_path / "aim-field.png")
        clicked = computer_use_tool.computer("click", x=x, y=y)
        print(pointer_words(clicked))
        typed = computer_use_tool.computer("type", text="hello")
        assert typed.startswith("Typed 5 characters."), typed
        wait_for("typed hello", f"The typing didn't reach the field. {pointer_words(clicked)}. {report}")
        assert service.status_snapshot()["app"] == "Smoke Notes"
    finally:
        service.stop()
        app.stdin.close()
        app.wait(timeout=10)
    assert stopped == []
