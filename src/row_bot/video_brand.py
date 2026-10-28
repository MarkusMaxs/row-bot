"""The brand kit (5.3.1 B3): one look every video project uses unless a version turns it off.

Its colour fills title cards and speed badges, its caption style sets how lines show under their
scenes, its logo (a picture from Library › Media) sits in a corner of the whole video, and its
intro and outro are title cards on its colour before the first scene and after the last. Its
music mood is what Row-Bot picks when it makes music for a video. It is kept in one small file in
the data folder.
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

logger = logging.getLogger(__name__)

CaptionStyle = Literal["box", "bold", "title"]
CAPTION_STYLES: tuple[str, ...] = ("box", "bold", "title")
MOODS: tuple[str, ...] = ("calm", "upbeat", "focused", "warm")
CORNERS: tuple[str, ...] = ("top-left", "top-right", "bottom-left", "bottom-right")
MAX_CARD = 120
_COLOUR = re.compile(r"#[0-9a-fA-F]{6}")
_FILE = "video-brand.json"


class BrandError(ValueError):
    """The kit can't be saved as asked; the message says why."""


@dataclass
class Brand:
    colour: str = "#1f2937"
    captions: str = "box"
    # A picture in the chats' files (Library › Media), by its reference; "" for none.
    logo: str = ""
    logo_corner: str = "top-left"
    intro: str = ""
    outro: str = ""
    music_mood: str = "calm"


def _path() -> Path:
    from row_bot.data_paths import get_row_bot_data_dir

    return get_row_bot_data_dir() / _FILE


def load() -> Brand:
    """The kit as saved, or the plain default."""
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return Brand()
    try:
        return _clean(data, check_logo=False)
    except BrandError:
        logger.warning("The saved brand kit was unreadable; using the default")
        return Brand()


def _clean(data: dict[str, Any], *, check_logo: bool) -> Brand:
    brand = Brand()
    colour = str(data.get("colour") or brand.colour)
    if not _COLOUR.fullmatch(colour):
        raise BrandError("The colour is a hex colour such as #1f2937.")
    captions = str(data.get("captions") or "box")
    if captions not in CAPTION_STYLES:
        raise BrandError(f"Captions are {', '.join(CAPTION_STYLES)}.")
    corner = str(data.get("logo_corner") or "top-left")
    if corner not in CORNERS:
        raise BrandError(f"The logo goes {', '.join(CORNERS)}.")
    mood = str(data.get("music_mood") or "calm")
    if mood not in MOODS:
        raise BrandError(f"The music mood is {', '.join(MOODS)}.")
    logo = str(data.get("logo") or "").strip()
    if logo and check_logo:
        _check_logo(logo)
    return Brand(colour=colour.lower(), captions=captions, logo=logo, logo_corner=corner,
                 intro=re.sub(r"\s+", " ", str(data.get("intro") or "")).strip()[:MAX_CARD],
                 outro=re.sub(r"\s+", " ", str(data.get("outro") or "")).strip()[:MAX_CARD], music_mood=mood)


def _check_logo(ref: str) -> None:
    from row_bot import media_library

    if not any(item["ref"] == ref and item["kind"] == "image" for item in media_library.items(limit=500)):
        raise BrandError("The logo is a picture from Library › Media.")


def save(data: dict[str, Any]) -> Brand:
    """Replace the kit (every field; a missing one is its default)."""
    brand = _clean(data, check_logo=True)
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex[:6]}.part")
    partial.write_text(json.dumps(asdict(brand)), encoding="utf-8", newline="\n")
    os.replace(partial, path)
    return brand


def logo_png(brand: Brand) -> bytes | None:
    """The logo as a PNG for the renderer, or None when there's none (or it can't be read any more)."""
    if not brand.logo:
        return None
    import io

    from PIL import Image

    from row_bot.application.attachments import AttachmentError, read_attachment

    try:
        picture = Image.open(io.BytesIO(read_attachment(brand.logo)[1])).convert("RGBA")
        output = io.BytesIO()
        picture.save(output, format="PNG")
        return output.getvalue()
    except (AttachmentError, OSError, ValueError):
        logger.info("The brand kit's logo couldn't be read")
        return None


def rgb(colour: str) -> tuple[int, int, int]:
    return int(colour[1:3], 16), int(colour[3:5], 16), int(colour[5:7], 16)
