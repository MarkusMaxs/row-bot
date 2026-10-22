"""The Stop bar on screen while Row-Bot uses the computer, and the person's own input.

While a computer use session runs, a slim always-on-top bar says "Row-Bot is
using Microsoft Edge" with Stop (Esc). Esc or Stop ends the run at once. When
the person moves the mouse, clicks, scrolls or types, Row-Bot pauses before
its next action and asks "Carry on?" in the chat.

The bar runs in a small helper process (``python -m
row_bot.computer_use.control_bar``) so its window and the input watch never
run inside the server, and Tk keeps the main thread it needs on macOS. The
server talks to it over stdin and stdout, one JSON object per line:

- to the helper: ``{"words": "...", "near": [x, y, w, h]}``, ``{"avoid": [x, y]}``,
  ``{"busy": true}`` (Row-Bot's own input is under way), ``{"quit": true}``;
- from the helper: ``{"event": "ready"}``, ``{"event": "stop", "by": "esc" | "button"}``,
  ``{"event": "input", "kind": "mouse" | "key"}``.

Windows: low-level mouse and keyboard hooks see every event with the flag
Windows sets on injected input, so Row-Bot's own input (Cua Driver's) is told
apart from the person's exactly. macOS: the HID system's idle time and key
state are polled, and input while Row-Bot's own action is under way is not
counted (unverified on a Mac: see the PR).
"""

from __future__ import annotations

import json
import logging
import os
import queue
import subprocess
import sys
import threading
import time
from typing import Any, Callable

logger = logging.getLogger(__name__)

# Mouse travel (pixels, summed) before it counts as the person's: a nudged desk isn't.
MOVE_THRESHOLD = 12
# The helper reports the person's input at most this often.
REPORT_INTERVAL = 0.25
ESC_VK = 0x1B
LLMHF_INJECTED = 0x01
LLKHF_INJECTED = 0x10
WM_KEYDOWN, WM_SYSKEYDOWN = 0x0100, 0x0104
WM_MOUSEMOVE = 0x0200
_MOUSE_BUTTONS = frozenset({0x0201, 0x0204, 0x0207, 0x020B, 0x020A, 0x020E})  # downs and wheels


class InputWatch:
    """Tells the person's own mouse and keyboard from Row-Bot's (pure: the OS glue feeds it)."""

    def __init__(self, emit: Callable[[dict[str, Any]], None], clock: Callable[[], float] = time.monotonic) -> None:
        self._emit = emit
        self._clock = clock
        self._last: tuple[int, int] | None = None
        self._travel = 0
        self._reported = -1e9
        self.busy = False

    def activity(self, kind: str = "mouse") -> None:
        """The person used the mouse or keyboard (reported at most every ``REPORT_INTERVAL``)."""

        now = self._clock()
        if now - self._reported >= REPORT_INTERVAL:
            self._reported = now
            self._emit({"event": "input", "kind": kind})

    def mouse(self, message: int, x: int, y: int, injected: bool) -> None:
        if injected:
            self._last = None  # Row-Bot moved the pointer: measure the person's travel from where it is now.
            return
        if message == WM_MOUSEMOVE:
            if self._last is not None:
                self._travel += abs(x - self._last[0]) + abs(y - self._last[1])
            self._last = (x, y)
            if self._travel < MOVE_THRESHOLD:
                return
            self._travel = 0
        elif message not in _MOUSE_BUTTONS:
            return
        self.activity("mouse")

    def key(self, message: int, vk: int, injected: bool) -> None:
        if injected or message not in {WM_KEYDOWN, WM_SYSKEYDOWN}:
            return
        if vk == ESC_VK:
            self._emit({"event": "stop", "by": "esc"})
            return
        self.activity("key")


# ── The server side ──────────────────────────────────────────────────────────


class ControlBar:
    """The helper process for one session: show the bar, move it, hear Stop and the person's input."""

    def __init__(
        self,
        *,
        on_stop: Callable[[str], None],
        on_input: Callable[[str], None],
        launch: Callable[[], subprocess.Popen] | None = None,
    ) -> None:
        self._on_stop = on_stop
        self._on_input = on_input
        self._launch = launch or _launch_helper
        self._process: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self.pid = 0

    def _send(self, message: dict[str, Any]) -> None:
        with self._lock:
            process = self._process
            if process is None and not message.get("quit"):
                try:
                    process = self._process = self._launch()
                except Exception:
                    logger.warning("The computer use Stop bar couldn't start", exc_info=True)
                    return
                self.pid = int(getattr(process, "pid", 0) or 0)
                threading.Thread(target=self._read, args=(process,), daemon=True,
                                 name="row-bot-stop-bar").start()
            if process is None or process.stdin is None:
                return
            try:
                process.stdin.write(json.dumps(message) + "\n")
                process.stdin.flush()
            except (OSError, ValueError):
                logger.debug("The Stop bar went away", exc_info=True)

    def show(self, words: str, near: tuple[float, float, float, float] | None = None) -> None:
        self._send({"words": str(words)[:120], "near": list(near) if near else None})

    def busy(self, flag: bool) -> None:
        self._send({"busy": bool(flag)})

    def avoid(self, point: tuple[float, float]) -> None:
        """Move the bar if it covers the point Row-Bot is about to click."""

        self._send({"avoid": [round(point[0]), round(point[1])]})

    def close(self) -> None:
        with self._lock:
            process, self._process = self._process, None
        if process is None:
            return
        try:
            if process.stdin is not None:
                process.stdin.write(json.dumps({"quit": True}) + "\n")
                process.stdin.close()
            process.wait(timeout=2)
        except Exception:
            process.kill()

    def _read(self, process: subprocess.Popen) -> None:
        for line in process.stdout or ():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            try:
                if event.get("event") == "stop":
                    self._on_stop(str(event.get("by") or "button"))
                elif event.get("event") == "input":
                    self._on_input(str(event.get("kind") or "mouse"))
            except Exception:
                logger.warning("The Stop bar's event wasn't handled", exc_info=True)


def _launch_helper() -> subprocess.Popen:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.Popen(  # noqa: S603 - this interpreter, a fixed module, no shell
        [sys.executable, "-m", "row_bot.computer_use.control_bar"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        creationflags=flags,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )


# ── The helper process ───────────────────────────────────────────────────────

_BACKGROUND = "#171C25"
_BORDER = "#3A4352"
_TEXT = "#E8EBF0"
_MUTED = "#98A2B1"
_ACCENT = "#78B8F2"
_STOP_BACKGROUND = "#491F29"
_STOP_HOVER = "#5E2733"
_STOP_TEXT = "#F4AFB8"


class _Output:
    def __init__(self) -> None:
        self._lock = threading.Lock()

    def __call__(self, message: dict[str, Any]) -> None:
        with self._lock:
            try:
                sys.stdout.write(json.dumps(message) + "\n")
                sys.stdout.flush()
            except (OSError, ValueError):
                pass


def _watch_windows(watch: InputWatch) -> None:  # pragma: no cover - needs a real Windows desktop
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    lresult = ctypes.c_ssize_t
    hookproc = ctypes.WINFUNCTYPE(lresult, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)

    class Keyboard(ctypes.Structure):
        _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD), ("flags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

    class Mouse(ctypes.Structure):
        _fields_ = [("pt", wintypes.POINT), ("mouseData", wintypes.DWORD), ("flags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

    user32.SetWindowsHookExW.argtypes = [ctypes.c_int, hookproc, wintypes.HINSTANCE, wintypes.DWORD]
    user32.SetWindowsHookExW.restype = wintypes.HHOOK
    user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
    user32.CallNextHookEx.restype = lresult
    user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
    kernel32.GetModuleHandleW.restype = wintypes.HMODULE

    def on_key(code: int, message: int, data: int) -> int:
        if code >= 0:
            event = ctypes.cast(data, ctypes.POINTER(Keyboard)).contents
            watch.key(int(message), int(event.vkCode), bool(event.flags & LLKHF_INJECTED))
        return user32.CallNextHookEx(None, code, message, data)

    def on_mouse(code: int, message: int, data: int) -> int:
        if code >= 0:
            event = ctypes.cast(data, ctypes.POINTER(Mouse)).contents
            watch.mouse(int(message), int(event.pt.x), int(event.pt.y), bool(event.flags & LLMHF_INJECTED))
        return user32.CallNextHookEx(None, code, message, data)

    keyboard, mouse = hookproc(on_key), hookproc(on_mouse)
    module = kernel32.GetModuleHandleW(None)
    hooks = [user32.SetWindowsHookExW(13, keyboard, module, 0), user32.SetWindowsHookExW(14, mouse, module, 0)]
    if not all(hooks):
        return
    message = wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
        pass


def _watch_macos(watch: InputWatch, stop: threading.Event) -> None:  # pragma: no cover - needs a Mac
    import ctypes
    import ctypes.util

    path = ctypes.util.find_library("CoreGraphics") or "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics"
    graphics = ctypes.CDLL(path)
    graphics.CGEventSourceSecondsSinceLastEventType.argtypes = [ctypes.c_int32, ctypes.c_uint32]
    graphics.CGEventSourceSecondsSinceLastEventType.restype = ctypes.c_double
    graphics.CGEventSourceKeyState.argtypes = [ctypes.c_int32, ctypes.c_uint16]
    graphics.CGEventSourceKeyState.restype = ctypes.c_bool
    hid, any_input, escape = 1, 0xFFFFFFFF, 53
    quiet_until = 0.0
    escape_down = False
    previous = graphics.CGEventSourceSecondsSinceLastEventType(hid, any_input)
    while not stop.wait(0.05):
        now = time.monotonic()
        if watch.busy:
            quiet_until = now + 0.4
        since = graphics.CGEventSourceSecondsSinceLastEventType(hid, any_input)
        fresh = since + 0.02 < previous
        previous = since
        pressed = bool(graphics.CGEventSourceKeyState(hid, escape))
        if now < quiet_until:
            escape_down = pressed
            continue
        if pressed and not escape_down:
            watch.key(WM_KEYDOWN, ESC_VK, False)
        elif fresh and not pressed:
            watch.activity("mouse")
        escape_down = pressed


def _monitor_area(near: list[float] | None) -> tuple[int, int, int, int, float] | None:
    """The work area (left, top, right, bottom) and scale of the monitor holding *near*, or the main one (Windows)."""

    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    class MonitorInfo(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT),
                    ("dwFlags", wintypes.DWORD)]

    user32 = ctypes.windll.user32
    user32.MonitorFromPoint.restype = wintypes.HMONITOR
    user32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
    centre = wintypes.POINT(int(near[0] + near[2] / 2), int(near[1] + near[3] / 2)) if near else wintypes.POINT(0, 0)
    monitor = user32.MonitorFromPoint(centre, 2 if near else 1)  # nearest, or the main monitor
    info = MonitorInfo()
    info.cbSize = ctypes.sizeof(MonitorInfo)
    if not user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
        return None
    scale = 1.0
    try:
        dpi_x, dpi_y = ctypes.c_uint(), ctypes.c_uint()
        ctypes.windll.shcore.GetDpiForMonitor(monitor, 0, ctypes.byref(dpi_x), ctypes.byref(dpi_y))
        scale = max(1.0, dpi_x.value / 96)
    except Exception:
        pass
    work = info.rcWork
    return work.left, work.top, work.right, work.bottom, scale


def main() -> None:  # pragma: no cover - the helper process; exercised by the platform smoke
    # The window in front (the app Row-Bot is about to use): Tk makes its own window the foreground one,
    # hidden, as it creates it, and the bar hands the foreground straight back before it shows.
    in_front = 0
    if sys.platform == "win32":
        import ctypes

        try:
            ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # per-monitor v2
        except Exception:
            pass
        in_front = ctypes.windll.user32.GetForegroundWindow()
    import tkinter as tk

    output = _Output()
    commands: queue.Queue[dict[str, Any]] = queue.Queue()
    watch = InputWatch(output)
    stopping = threading.Event()

    def read() -> None:
        for line in sys.stdin:
            try:
                commands.put(json.loads(line))
            except ValueError:
                continue
        commands.put({"quit": True})

    root = tk.Tk()
    root.withdraw()
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    root.title("Row-Bot computer use")
    root.configure(background=_BORDER)
    if sys.platform == "darwin":
        try:
            root.tk.call("::tk::unsupported::MacWindowStyle", "style", root._w, "help", "noActivates")
            from AppKit import NSApplication

            NSApplication.sharedApplication().setActivationPolicy_(1)  # no Dock icon
        except Exception:
            pass
    family = "Segoe UI" if sys.platform == "win32" else "Helvetica"
    point = root.winfo_fpixels("1p")  # Sizes in points, so the bar is the same size at any display scale.
    body = tk.Frame(root, background=_BACKGROUND, padx=round(10 * point), pady=round(5 * point))
    body.pack(fill="both", expand=True, padx=1, pady=1)
    size = round(7 * point)
    dot = tk.Canvas(body, width=size, height=size, background=_BACKGROUND, highlightthickness=0)
    dot.create_oval(1, 1, size - 1, size - 1, fill=_ACCENT, outline="")
    dot.pack(side="left", padx=(0, round(8 * point)))
    words = tk.Label(body, text="Row-Bot is using your computer", background=_BACKGROUND, foreground=_TEXT,
                     font=(family, 10))
    words.pack(side="left")
    tk.Label(body, text="Esc to stop", background=_BACKGROUND, foreground=_MUTED,
             font=(family, 9)).pack(side="left", padx=round(10 * point))

    def press_stop(_event: Any = None) -> None:
        output({"event": "stop", "by": "button"})

    stop = tk.Label(body, text="Stop", background=_STOP_BACKGROUND, foreground=_STOP_TEXT, cursor="hand2",
                    font=(family, 10, "bold"), padx=round(10 * point), pady=round(2 * point))
    stop.pack(side="left")
    stop.bind("<Button-1>", press_stop)
    stop.bind("<Enter>", lambda _e: stop.configure(background=_STOP_HOVER))
    stop.bind("<Leave>", lambda _e: stop.configure(background=_STOP_BACKGROUND))

    state: dict[str, Any] = {"near": None, "top": False, "rect": (0, 0, 0, 0), "shown": False}

    def place() -> None:
        root.update_idletasks()
        width, height = root.winfo_reqwidth(), root.winfo_reqheight()
        area = _monitor_area(state["near"])
        if area is None:
            left, top, right, bottom, scale = 0, 0, root.winfo_screenwidth(), root.winfo_screenheight(), 1.0
        else:
            left, top, right, bottom, scale = area
        margin = round(20 * scale)
        x = left + (right - left - width) // 2
        y = top + margin if state["top"] else bottom - height - margin
        root.geometry(f"{width}x{height}+{x}+{y}")
        state["rect"] = (x, y, x + width, y + height)

    def style_window() -> None:
        if sys.platform != "win32":
            return
        import ctypes

        user32 = ctypes.windll.user32
        hwnd = user32.GetParent(root.winfo_id()) or root.winfo_id()
        exstyle = user32.GetWindowLongW(hwnd, -20)
        user32.SetWindowLongW(hwnd, -20, exstyle | 0x08000000 | 0x00000080 | 0x00000008)  # no-activate, tool, top
        try:
            preference = ctypes.c_int(2)  # rounded corners on Windows 11
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(preference), 4)
        except Exception:
            pass

    def poll() -> None:
        while True:
            try:
                command = commands.get_nowait()
            except queue.Empty:
                break
            if command.get("quit"):
                stopping.set()
                root.destroy()
                return
            if "words" in command:
                words.configure(text=str(command.get("words") or "")[:120])
                state["near"] = command.get("near") or state["near"]
                place()
                if not state["shown"]:
                    root.deiconify()
                    root.update_idletasks()
                    place()
                    state["shown"] = True
                    output({"event": "ready", "pid": os.getpid(), "rect": list(state["rect"])})
            if "busy" in command:
                watch.busy = bool(command["busy"])
            if command.get("avoid"):
                px, py = command["avoid"]
                x0, y0, x1, y1 = state["rect"]
                if x0 - 16 <= px <= x1 + 16 and y0 - 16 <= py <= y1 + 16:
                    state["top"] = not state["top"]
                    place()
        root.after(40, poll)

    # Tk makes the bar's frame window now; it is styled never to activate and gives the foreground back.
    root.update_idletasks()
    style_window()
    if sys.platform == "win32" and in_front:
        import ctypes

        user32 = ctypes.windll.user32
        if user32.GetForegroundWindow() == (user32.GetParent(root.winfo_id()) or root.winfo_id()):
            user32.SetForegroundWindow(in_front)
    threading.Thread(target=read, daemon=True).start()
    if sys.platform == "win32":
        threading.Thread(target=_watch_windows, args=(watch,), daemon=True).start()
    elif sys.platform == "darwin":
        threading.Thread(target=_watch_macos, args=(watch, stopping), daemon=True).start()
    root.after(0, poll)
    root.mainloop()


if __name__ == "__main__":  # pragma: no cover
    main()
