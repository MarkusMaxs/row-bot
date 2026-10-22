"""The real Cua Driver on a fixture window of this test's own: a short task, end to end (Windows).

Opt-in and slow: set ``ROW_BOT_CUA_DRIVER`` to a reviewed ``cua-driver.exe``
(0.20.0). The test opens its own small window ("Smoke Notes": a magenta Count
button and a cyan field), finds them in the picture the model would get,
clicks Count and types into the field with real input through the
``computer`` tool, with the real Stop bar showing. It touches nothing else on
the desktop. Cua's telemetry is turned off for this child process.
"""

from __future__ import annotations

import io
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


@pytest.mark.skipif(sys.platform != "win32" or not Path(DRIVER).is_file(),
                    reason="set ROW_BOT_CUA_DRIVER to a cua-driver.exe on Windows")
def test_a_short_task_on_a_real_window_with_real_input(tmp_path, monkeypatch):
    if _screen_locked():
        pytest.skip("the screen is locked: real input can't reach a window")
    from row_bot import agent
    from row_bot.browser import model_view
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

    app = subprocess.Popen([sys.executable, "-I", str(FIXTURE)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                           text=True, encoding="utf-8")
    lines: queue.Queue[str] = queue.Queue()
    threading.Thread(target=lambda: [lines.put(line.strip()) for line in app.stdout], daemon=True).start()

    def wait_for(text: str) -> None:
        while True:
            line = lines.get(timeout=15)
            if line == text:
                return

    try:
        wait_for("ready")
        listed = computer_use_tool.computer("list_apps")
        assert '"Smoke Notes"' in listed

        shown = computer_use_tool.computer("screenshot", app="Smoke Notes")
        assert shown.startswith("Took a screenshot.\n"), shown
        x, y = _find(service.latest().image, (255, 0, 255))
        clicked = computer_use_tool.computer("click", x=x, y=y)
        assert clicked.startswith(f"Clicked at {x},{y}"), clicked
        wait_for("clicked 1")

        x, y = _find(service.latest().image, (0, 255, 255))
        computer_use_tool.computer("click", x=x, y=y)
        typed = computer_use_tool.computer("type", text="hello")
        assert typed.startswith("Typed 5 characters."), typed
        wait_for("typed hello")
        assert service.status_snapshot()["app"] == "Smoke Notes"
    finally:
        service.stop()
        app.stdin.close()
        app.wait(timeout=10)
    assert stopped == []
