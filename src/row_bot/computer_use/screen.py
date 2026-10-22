"""One window's picture as the model sees it, and the way back to the screen.

Cua Driver captures a window and may scale its picture down; its pointer
input takes coordinates in that picture and maps them to the screen itself
(DPI and every monitor included). Row-Bot scales the picture once more for
the model (at most 1280×800), so a point the model names maps back by one
exact ratio: ``cua = model × cua_size / model_size``.

Controls come from the window's accessibility tree in screen coordinates;
they map into the model's picture through the window's bounds on screen.
"""

from __future__ import annotations

import io
import time
from dataclasses import dataclass, field

from row_bot.computer_use.client import CuaElement
from row_bot.computer_use.policy import Control, role_of, secret_field

MODEL_MAX_WIDTH = 1280
MODEL_MAX_HEIGHT = 800
THUMB_MAX_WIDTH = 640

# Plain words for the roles the controls list names.
_ROLE_WORDS = {
    "button": "button", "splitbutton": "button", "popupbutton": "button", "menubutton": "button",
    "togglebutton": "toggle", "switch": "toggle", "hyperlink": "link", "link": "link", "menuitem": "menu item",
    "checkbox": "checkbox", "radiobutton": "radio", "radio": "radio", "tabitem": "tab", "tab": "tab",
    "listitem": "item", "treeitem": "item", "option": "option", "combobox": "dropdown", "edit": "field",
    "textfield": "field", "textbox": "field", "textarea": "field", "searchfield": "search field",
    "securetextfield": "password field", "slider": "slider", "spinner": "number field", "spinbutton": "number field",
    "menubaritem": "menu", "dataitem": "cell", "cell": "cell",
}


@dataclass(frozen=True)
class Window:
    """A top-level window on screen. ``bounds`` are the window's frame in screen coordinates."""

    pid: int
    window_id: int
    app: str
    title: str
    bounds: tuple[float, float, float, float]
    minimized: bool = False
    front: int = 0


@dataclass
class Shot:
    """A window's picture: the driver's, the model's (scaled once) and its controls."""

    window: Window
    cua_size: tuple[int, int]
    image: bytes = field(repr=False)
    size: tuple[int, int]
    elements: tuple[CuaElement, ...] = ()
    taken: float = field(default_factory=time.monotonic)
    _thumb: bytes | None = field(default=None, repr=False)

    # ── Coordinates ──────────────────────────────────────────────────────────

    def contains(self, x: float, y: float) -> bool:
        return 0 <= x < self.size[0] and 0 <= y < self.size[1]

    def to_cua(self, x: float, y: float) -> tuple[float, float]:
        """A point in the model's picture → the same point in the driver's picture."""

        return (round(x * self.cua_size[0] / self.size[0], 2), round(y * self.cua_size[1] / self.size[1], 2))

    def from_screen(self, sx: float, sy: float) -> tuple[int, int]:
        """A screen point → the model's picture (through the window's frame on screen)."""

        left, top, width, height = self.window.bounds
        if width <= 0 or height <= 0:
            return -1, -1
        return round((sx - left) * self.size[0] / width), round((sy - top) * self.size[1] / height)

    def to_screen(self, x: float, y: float) -> tuple[float, float]:
        left, top, width, height = self.window.bounds
        return left + x * width / self.size[0], top + y * height / self.size[1]

    # ── Controls ─────────────────────────────────────────────────────────────

    def _box(self, element: CuaElement) -> tuple[int, int, int, int]:
        x, y, width, height = element.bounds
        left, top = self.from_screen(x, y)
        right, bottom = self.from_screen(x + width, y + height)
        return left, top, right, bottom

    def control_at(self, x: float, y: float) -> Control | None:
        """The control under a point: the smallest one there that a click acts on, named by what it says."""

        hits: list[tuple[int, CuaElement]] = []
        for element in self.elements:
            left, top, right, bottom = self._box(element)
            if left <= x < right and top <= y < bottom and right > left and bottom > top:
                hits.append(((right - left) * (bottom - top), element))
        if not hits:
            return None
        hits.sort(key=lambda hit: hit[0])
        actionable = [element for _area, element in hits if role_of(element.role) in _ROLE_WORDS]
        chosen = actionable[0] if actionable else hits[0][1]
        label = chosen.label or next((element.label for _area, element in hits if element.label), "")
        return Control(role=chosen.role, label=label)

    def controls(self, budget: int) -> str:
        """``button "Leak" 640,412; field "Postcode" 400,220`` within *budget* characters."""

        seen: set[tuple[str, str]] = set()
        rows: list[tuple[int, int, int, str]] = []
        for element in self.elements:
            words = _ROLE_WORDS.get(role_of(element.role))
            label = " ".join(str(element.label or "").split())[:60]
            if not words or not label or element.enabled is False:
                continue
            left, top, right, bottom = self._box(element)
            cx, cy = (left + right) // 2, (top + bottom) // 2
            if right <= left or bottom <= top or not self.contains(cx, cy):
                continue
            if (words, label.casefold()) in seen:
                continue
            seen.add((words, label.casefold()))
            label = label.replace('"', "'")
            rows.append((0 if element.in_web_content else 1, cy, cx, f'{words} "{label}" {cx},{cy}'))
        rows.sort(key=lambda row: (row[0], row[1] // 8, row[2]))
        out: list[str] = []
        used = 0
        for _web, _y, _x, line in rows:
            if used + len(line) + 2 > budget:
                break
            out.append(line)
            used += len(line) + 2
        if len(out) < len(rows):
            out.append(f"{len(rows) - len(out)} more")
        return "; ".join(out)

    def labels(self) -> list[str]:
        return [element.label for element in self.elements if element.label]

    # ── Pictures ─────────────────────────────────────────────────────────────

    def thumbnail(self, mark: tuple[float, float] | None = None) -> bytes:
        """A small JPEG for the chat (with a ring where Row-Bot is about to act, when asked)."""

        if mark is None and self._thumb is not None:
            return self._thumb
        from PIL import Image, ImageDraw

        with Image.open(io.BytesIO(self.image)) as source:
            picture = source.convert("RGB")
        scale = min(1.0, THUMB_MAX_WIDTH / picture.width)
        if scale < 1.0:
            picture = picture.resize((max(1, round(picture.width * scale)), max(1, round(picture.height * scale))))
        if mark is not None:
            x, y = mark[0] * picture.width / self.size[0], mark[1] * picture.height / self.size[1]
            radius = max(8, picture.width // 40)
            draw = ImageDraw.Draw(picture)
            for width, colour in ((5, (255, 255, 255)), (3, (220, 38, 38))):
                draw.ellipse((x - radius, y - radius, x + radius, y + radius), outline=colour, width=width)
        out = io.BytesIO()
        picture.save(out, "JPEG", quality=72)
        data = out.getvalue()
        if mark is None:
            self._thumb = data
        return data


def take(window: Window, png: bytes, cua_size: tuple[int, int], elements: tuple[CuaElement, ...]) -> Shot:
    """The model's picture of a window: the driver's scaled to fit 1280×800 (never up), as a JPEG.

    Password, code and card fields are covered, as the browser covers them.
    """

    from PIL import Image, ImageDraw

    with Image.open(io.BytesIO(png)) as source:
        picture = source.convert("RGB")
    scale = min(1.0, MODEL_MAX_WIDTH / picture.width, MODEL_MAX_HEIGHT / picture.height)
    if scale < 1.0:
        picture = picture.resize(
            (max(1, round(picture.width * scale)), max(1, round(picture.height * scale))), Image.LANCZOS,
        )
    shot = Shot(window, cua_size, b"", picture.size, elements)
    draw = ImageDraw.Draw(picture)
    for element in elements:
        if secret_field(element.role, element.label):
            left, top, right, bottom = shot._box(element)
            if right > left and bottom > top:
                draw.rectangle((left, top, right, bottom), fill=(96, 104, 116))
    out = io.BytesIO()
    picture.save(out, "JPEG", quality=80)
    shot.image = out.getvalue()
    return shot
