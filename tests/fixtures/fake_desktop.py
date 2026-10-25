"""A fake desktop behind a fake Cua Driver, for computer use tests.

Windows have a frame on the screen (physical pixels, any monitor, negative
coordinates included) and controls inside it. The fake answers Cua Driver
0.20.0's tools the way the real one does on Windows:

- ``get_window_state`` pictures the window's client bitmap (its frame less a
  one-pixel inset), scaled down to at most 1568 pixels like Cua's default,
  and lists the controls with screen-coordinate frames;
- pointer input takes window-local picture coordinates and maps them back to
  the screen with the ratio of the last picture of that process, plus the
  inset (Cua's ``bitmap_to_screen``);
- foreground input lands in whichever window is in front, so a click on a
  window Row-Bot didn't bring forward goes astray, as it would for real.

A window Windows scales for its display (a DPI-unaware app: ``window_dpi``
below its ``monitor_dpi``) draws itself at its own smaller size into the
top-left corner of Cua's bitmap, the rest black, while Cua still maps picture
points 1:1 from the frame: that is what Cua Driver 0.20.0 does with a Tk
window at 150 %. Its controls' boxes are where it drew them in that bitmap.

Every input is recorded with the screen point it reached; the pointer stays
where the last click put it.
"""

from __future__ import annotations

import base64
import io
from dataclasses import dataclass, field
from typing import Any, Callable

from row_bot.computer_use.display import WindowDisplay
from row_bot.mcp_client.results import RawCallContent, RawCallResult

CUA_MAX_DIMENSION = 1568
INSET = 1


@dataclass
class FakeControl:
    role: str
    label: str
    box: tuple[float, float, float, float]  # x, y, width, height where the window drew it in Cua's bitmap
    on_click: Callable[["FakeDesktop", "FakeWindow"], None] | None = None
    value: str = ""
    colour: tuple[int, int, int] | None = None  # filled with it in the picture, for a test to find it by eye

    def centre(self) -> tuple[int, int]:
        x, y, width, height = self.box
        return x + width // 2, y + height // 2


@dataclass
class FakeWindow:
    pid: int
    window_id: int
    process: str
    title: str
    bounds: tuple[int, int, int, int]  # the frame on screen: x, y, width, height
    controls: list[FakeControl] = field(default_factory=list)
    minimized: bool = False
    window_dpi: int = 96
    monitor_dpi: int = 96
    border: tuple[int, int] = (0, 0)  # the invisible border: the frame's origin less the window rectangle's

    @property
    def scale(self) -> float:
        return self.monitor_dpi / self.window_dpi

    def screen_point(self, local: tuple[float, float]) -> tuple[float, float]:
        """Where a point the window drew at *local* in Cua's bitmap really is on screen."""

        left, top = self.bounds[0] - self.border[0], self.bounds[1] - self.border[1]
        return (left + (local[0] + self.border[0] + INSET) * self.scale,
                top + (local[1] + self.border[1] + INSET) * self.scale)

    def local_point(self, screen: tuple[float, float]) -> tuple[float, float]:
        left, top = self.bounds[0] - self.border[0], self.bounds[1] - self.border[1]
        return ((screen[0] - left) / self.scale - self.border[0] - INSET,
                (screen[1] - top) / self.scale - self.border[1] - INSET)

    def cua_screen_point(self, local: tuple[float, float]) -> tuple[float, float]:
        """Where Cua sends a point of its bitmap: the frame's origin, the inset, then 1:1."""

        return self.bounds[0] + INSET + local[0], self.bounds[1] + INSET + local[1]

    def drawn_size(self) -> tuple[float, float]:
        """How much of Cua's bitmap the window drew."""

        width, height = self.bitmap_size()
        return (min(width, (self.bounds[2] + self.border[0]) / self.scale - self.border[0] - INSET),
                min(height, (self.bounds[3] + self.border[1]) / self.scale - self.border[1] - INSET))

    def bitmap_size(self) -> tuple[int, int]:
        return self.bounds[2] - 2 * INSET, self.bounds[3] - 2 * INSET

    def control(self, label: str) -> FakeControl:
        return next(control for control in self.controls if control.label == label)


@dataclass
class FakeDesktop:
    windows: list[FakeWindow] = field(default_factory=list)
    apps: dict[int, str] = field(default_factory=dict)  # pid → the name list_apps gives it
    launchable: dict[str, Callable[["FakeDesktop"], FakeWindow]] = field(default_factory=dict)
    permission_denied: bool = False
    # Windows keeps another window in front (a locked screen): no window comes forward, no input is sent.
    foreground_refused: bool = False

    def __post_init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.inputs: list[dict[str, Any]] = []
        self.typed: list[tuple[int, str]] = []
        self.ratios: dict[int, float] = {}
        self.focused: FakeControl | None = None
        self.pointer: tuple[int, int] | None = None
        # Where the pointer ends up past the point Cua sent it to (a driver that scales points itself).
        self.pointer_drift: tuple[int, int] = (0, 0)
        self.opened = False
        self.closed = False
        self.front = self.windows[0].window_id if self.windows else 0

    # ── The transport ────────────────────────────────────────────────────────

    def open(self) -> None:
        self.opened = True

    def close(self) -> None:
        self.closed = True

    def window(self, window_id: int) -> FakeWindow:
        return next(window for window in self.windows if window.window_id == window_id)

    def front_window(self) -> FakeWindow:
        return self.window(self.front)

    # ── What Windows says (``display``) ──────────────────────────────────────

    def window_display(self, window_id: int) -> WindowDisplay | None:
        window = next((window for window in self.windows if window.window_id == window_id), None)
        if window is None:
            return None
        left, top, width, height = window.bounds
        border_x, border_y = window.border
        return WindowDisplay(rect=(left - border_x, top - border_y, left + width + border_x, top + height + border_x),
                             frame=(left, top, left + width, top + height),
                             window_dpi=window.window_dpi, monitor_dpi=window.monitor_dpi)

    def cursor_position(self) -> tuple[int, int] | None:
        return self.pointer

    def call_raw(self, name: str, arguments: dict[str, Any] | None = None) -> RawCallResult:
        args = dict(arguments or {})
        recorded = {key: ("<typed>" if key == "text" else value) for key, value in args.items()}
        self.calls.append((name, recorded))
        if self.permission_denied and name not in {"start_session", "end_session"}:
            return self._error("permission_denied", "Accessibility permission is missing")
        if self.foreground_refused and name in {"bring_to_front", "click", "double_click", "right_click", "drag",
                                                "type_text", "press_key", "hotkey", "scroll"}:
            return self._error("foreground_unavailable", f"{name}: Windows kept foreground on another window")
        handler = getattr(self, f"_{name}", None)
        if handler is None:
            return self._error("unsupported", f"unknown tool {name}")
        return handler(args)

    @staticmethod
    def _result(structured: dict[str, Any], *, text: str = "ok", image: bytes | None = None) -> RawCallResult:
        content = [RawCallContent(kind="text", text=text)]
        if image is not None:
            content.append(RawCallContent(kind="image", data=base64.b64encode(image).decode("ascii"),
                                          mime_type="image/png"))
        return RawCallResult(tuple(content), structured, False)

    @staticmethod
    def _error(code: str, message: str) -> RawCallResult:
        return RawCallResult((RawCallContent(kind="text", text=message),), {"error": {"code": code}}, True)

    # ── Session and windows ──────────────────────────────────────────────────

    def _start_session(self, args: dict[str, Any]) -> RawCallResult:
        return self._result({"session": args.get("session"), "ok": True})

    def _end_session(self, args: dict[str, Any]) -> RawCallResult:
        return self._result({"session": args.get("session"), "ok": True})

    def _list_apps(self, _args: dict[str, Any]) -> RawCallResult:
        return self._result({"apps": [
            {"pid": pid, "name": name, "running": True, "active": any(
                window.pid == pid and window.window_id == self.front for window in self.windows)}
            for pid, name in self.apps.items()
        ]})

    def _list_windows(self, args: dict[str, Any]) -> RawCallResult:
        order = sorted(self.windows, key=lambda window: window.window_id != self.front)
        rows = []
        for index, window in enumerate(order):
            if args.get("pid") and window.pid != args["pid"]:
                continue
            x, y, width, height = window.bounds
            rows.append({
                "window_id": window.window_id, "pid": window.pid, "app_name": window.process, "title": window.title,
                "bounds": {"x": x, "y": y, "width": width, "height": height}, "layer": 0,
                "z_index": len(order) - index, "is_on_screen": not window.minimized, "minimized": window.minimized,
            })
        return self._result({"windows": rows})

    def _launch_app(self, args: dict[str, Any]) -> RawCallResult:
        make = self.launchable.get(str(args.get("name") or ""))
        if make is None:
            return self._error("not_found", "no such app")
        window = make(self)
        self.windows.append(window)
        return self._result({"pid": window.pid, "name": window.process, "windows": []})

    def _bring_to_front(self, args: dict[str, Any]) -> RawCallResult:
        if not any(window.window_id == args.get("window_id") for window in self.windows):
            return self._error("not_found", "no such window")
        window = self.window(args["window_id"])
        window.minimized = False
        self.front = window.window_id
        return self._result({"now_fg_hwnd": window.window_id})

    # ── Pictures ─────────────────────────────────────────────────────────────

    def picture(self, window: FakeWindow) -> bytes:
        from PIL import Image, ImageDraw

        image = Image.new("RGB", window.bitmap_size(), (0, 0, 0))
        draw = ImageDraw.Draw(image)
        drawn_width, drawn_height = window.drawn_size()
        draw.rectangle((0, 0, drawn_width - 1, drawn_height - 1), fill=(250, 250, 250))
        draw.text((10, 10), window.title, fill=(20, 20, 20))
        for control in window.controls:
            x, y, width, height = control.box
            draw.rectangle((x, y, x + width, y + height), outline=(40, 90, 160), width=2, fill=control.colour)
            draw.text((x + 6, y + 6), control.label, fill=(20, 20, 20))
        out = io.BytesIO()
        image.save(out, "PNG")
        return out.getvalue()

    def _get_window_state(self, args: dict[str, Any]) -> RawCallResult:
        if not any(window.window_id == args.get("window_id") for window in self.windows):
            return self._error("not_found", f"No window with window_id {args.get('window_id')} exists.")
        window = self.window(args["window_id"])
        if window.minimized:
            return self._error("minimized", "screenshot unavailable: minimized window")
        from PIL import Image

        png = self.picture(window)
        width, height = window.bitmap_size()
        scale = min(1.0, CUA_MAX_DIMENSION / max(width, height))
        if scale < 1.0:
            with Image.open(io.BytesIO(png)) as source:
                resized = source.resize((round(width * scale), round(height * scale)))
            out = io.BytesIO()
            resized.save(out, "PNG")
            png = out.getvalue()
            self.ratios[window.pid] = width / resized.width
            width, height = resized.size
        else:
            self.ratios.pop(window.pid, None)
        limit = int(args.get("max_elements") or 5000)
        elements = []
        for index, control in enumerate(window.controls[:limit]):
            x, y = window.screen_point(control.box[:2])
            elements.append({
                "element_index": index, "element_token": f"s1:{index}", "role": control.role, "label": control.label,
                "depth": 1, "frame": {"x": x, "y": y, "w": control.box[2] * window.scale,
                                      "h": control.box[3] * window.scale}, "enabled": True,
                **({"value": control.value} if control.value else {}),
            })
        return self._result({"pid": window.pid, "window_id": window.window_id, "elements": elements,
                             "screenshot_width": width, "screenshot_height": height},
                            image=png if args.get("include_screenshot", True) else None)

    # ── Input ────────────────────────────────────────────────────────────────

    def _to_screen(self, args: dict[str, Any], x_key: str = "x", y_key: str = "y") -> tuple[float, float]:
        window = self.window(args["window_id"])
        ratio = self.ratios.get(window.pid, 1.0)
        # Cua truncates the scaled point to whole pixels before mapping it.
        return window.cua_screen_point((int(float(args[x_key]) * ratio), int(float(args[y_key]) * ratio)))

    def _land(self, kind: str, screen: tuple[float, float], args: dict[str, Any]) -> RawCallResult:
        """Real input lands in the front window, at a screen point (the pointer goes there first)."""

        screen = (screen[0] + self.pointer_drift[0], screen[1] + self.pointer_drift[1])
        self.pointer = (int(screen[0]), int(screen[1]))
        target = self.front_window()
        local = target.local_point(screen)
        hit = next((control for control in target.controls
                    if control.box[0] <= local[0] < control.box[0] + control.box[2]
                    and control.box[1] <= local[1] < control.box[1] + control.box[3]), None)
        self.inputs.append({"kind": kind, "screen": screen, "window_id": target.window_id,
                            "control": hit.label if hit else "", "delivery": args.get("delivery_mode")})
        if hit is not None:
            self.focused = hit
            if hit.on_click is not None and kind in {"click", "double_click"}:
                hit.on_click(self, target)
        return self._result({"effect": "unverifiable"}, text=f"Clicked at {screen}")

    def _click(self, args: dict[str, Any]) -> RawCallResult:
        return self._land("click", self._to_screen(args), args)

    def _double_click(self, args: dict[str, Any]) -> RawCallResult:
        return self._land("double_click", self._to_screen(args), args)

    def _right_click(self, args: dict[str, Any]) -> RawCallResult:
        return self._land("right_click", self._to_screen(args), args)

    def _drag(self, args: dict[str, Any]) -> RawCallResult:
        start, end = self._to_screen(args, "from_x", "from_y"), self._to_screen(args, "to_x", "to_y")
        self.inputs.append({"kind": "drag", "screen": start, "end": end, "window_id": self.front,
                            "delivery": args.get("delivery_mode")})
        return self._result({"effect": "unverifiable"})

    def _type_text(self, args: dict[str, Any]) -> RawCallResult:
        self.typed.append((self.front, str(args.get("text") or "")))
        if self.focused is not None:
            self.focused.value += str(args.get("text") or "")
        self.inputs.append({"kind": "type", "window_id": self.front, "delivery": args.get("delivery_mode")})
        return self._result({"effect": "unverifiable"})

    def _press_key(self, args: dict[str, Any]) -> RawCallResult:
        self.inputs.append({"kind": "key", "keys": [args.get("key")], "window_id": self.front,
                            "delivery": args.get("delivery_mode")})
        return self._result({"effect": "unverifiable"})

    def _hotkey(self, args: dict[str, Any]) -> RawCallResult:
        self.inputs.append({"kind": "key", "keys": list(args.get("keys") or []), "window_id": self.front,
                            "delivery": args.get("delivery_mode")})
        return self._result({"effect": "unverifiable"})

    def _scroll(self, args: dict[str, Any]) -> RawCallResult:
        self.inputs.append({"kind": "scroll", "direction": args.get("direction"), "amount": args.get("amount"),
                            "window_id": self.front, "delivery": args.get("delivery_mode")})
        return self._result({"effect": "unverifiable"})


class FakeBar:
    """The Stop bar without a window: what it was told, and the person's hand on the mouse."""

    instances: list["FakeBar"] = []

    def __init__(self, *, on_stop: Callable[[str], None], on_input: Callable[[str], None]) -> None:
        self.on_stop, self.on_input = on_stop, on_input
        self.shown: list[str] = []
        self.avoided: list[tuple[float, float]] = []
        self.busy_flags: list[bool] = []
        self.closed = False
        self.pid = 0
        FakeBar.instances.append(self)

    def show(self, words: str, near: Any = None) -> None:
        self.shown.append(words)

    def busy(self, flag: bool) -> None:
        self.busy_flags.append(flag)

    def avoid(self, point: tuple[float, float]) -> None:
        self.avoided.append(point)

    def close(self) -> None:
        self.closed = True

    def person_moves_the_mouse(self) -> None:
        self.on_input("mouse")

    def press(self, by: str = "button") -> None:
        self.on_stop(by)
