"""Pictures in captured messages: kept as their own blobs, never as base64 inside a message (P2, O128).

5.3 sends a model that sees images the page's (or the window's) picture after each browser or
computer action, as one extra message: a caption and an ``image_url`` part with a ``data:`` URI.

- **Capture** (:func:`split`) moves each such picture out of the message before the message is
  redacted and stored: the original image bytes become a blob of their own (deduplicated by their
  SHA-256), and the part keeps only a reference (:data:`MARKER`). Redaction therefore never scans or
  rewrites picture data, and a message blob holds no base64.
- **Reading back** (:func:`join`) puts the bytes back as the same ``data:`` URI, so the request is
  rebuilt exactly as the model saw it. Only a URI whose base64 is canonical is moved out, so rebuilding
  gives the same text.
- A picture dropped under the size cap stays a reference marked ``dropped``: the message and its
  caption stay, the request is no longer exact.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
from collections.abc import Callable, Mapping
from typing import Any

#: The key a stored ``image_url`` part holds instead of its URL.
MARKER = "row_bot_picture"
_DATA_URI = re.compile(r"data:(image/[A-Za-z0-9.+-]{1,64});base64,([A-Za-z0-9+/]*={0,2})\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def _moved(image_url: Any, found: dict[str, bytes]) -> dict[str, Any] | None:
    """The stored form of one ``image_url`` value (its bytes added to ``found``), or None to keep it as it is."""

    text = image_url if isinstance(image_url, str) else (image_url.get("url") if isinstance(image_url, Mapping) else None)
    match = _DATA_URI.match(text) if isinstance(text, str) else None
    if match is None:
        return None
    try:
        data = base64.b64decode(match.group(2), validate=True)
    except (binascii.Error, ValueError):
        return None
    if not data or base64.b64encode(data).decode("ascii") != match.group(2):
        return None  # not canonical: rebuilding wouldn't give the same text
    sha = hashlib.sha256(data).hexdigest()
    found[sha] = data
    reference: dict[str, Any] = {"sha256": sha, "media_type": match.group(1)}
    if isinstance(image_url, str):
        return {MARKER: {**reference, "form": "text"}}
    return {**{key: value for key, value in image_url.items() if key != "url"}, MARKER: reference}


def split(value: Any) -> tuple[Any, dict[str, bytes]]:
    """``value`` (a message as plain data) with every inline picture moved out, and the pictures by hash."""

    found: dict[str, bytes] = {}

    def walk(item: Any) -> Any:
        if isinstance(item, Mapping):
            if item.get("type") == "image_url" and "image_url" in item:
                moved = _moved(item["image_url"], found)
                if moved is not None:
                    return {**{key: walk(part) for key, part in item.items() if key != "image_url"},
                            "image_url": moved}
            return {key: walk(part) for key, part in item.items()}
        if isinstance(item, list):
            return [walk(part) for part in item]
        if isinstance(item, tuple):
            return tuple(walk(part) for part in item)
        return item

    return walk(value), found


class Missing(Exception):
    """A picture a message names is gone and wasn't dropped (purged or damaged)."""


def join(value: Any, read: Callable[[str], bytes | None], dropped: Callable[[str], bool]) -> tuple[Any, int]:
    """A stored message with its pictures put back as they were sent, and how many were dropped under the
    size cap (those stay references, marked ``dropped``). Raises :class:`Missing` for any other gone picture."""

    lost = [0]

    def walk(item: Any) -> Any:
        if isinstance(item, Mapping):
            image_url = item.get("image_url")
            if item.get("type") == "image_url" and isinstance(image_url, Mapping) and MARKER in image_url:
                return {**{key: walk(part) for key, part in item.items() if key != "image_url"},
                        "image_url": _rebuilt(image_url)}
            return {key: walk(part) for key, part in item.items()}
        if isinstance(item, list):
            return [walk(part) for part in item]
        return item

    def _rebuilt(image_url: Mapping[str, Any]) -> Any:
        reference = image_url.get(MARKER)
        sha = str(reference.get("sha256") or "") if isinstance(reference, Mapping) else ""
        data = read(sha) if _SHA256.fullmatch(sha) else None
        if data is None:
            if not _SHA256.fullmatch(sha) or not dropped(sha):
                raise Missing(sha[:12])
            lost[0] += 1
            return {**image_url, MARKER: {**reference, "dropped": True}}
        url = f"data:{reference.get('media_type') or 'image/jpeg'};base64,{base64.b64encode(data).decode('ascii')}"
        if reference.get("form") == "text":
            return url
        return {**{key: part for key, part in image_url.items() if key != MARKER}, "url": url}

    return walk(value), lost[0]


def is_page_picture(message: Mapping[str, Any]) -> bool:
    """The browser's or the computer's picture after an action (5.3, ``browser.model_view``): one extra human
    message with no id whose content has an ``image_url`` part, sent only to a model that sees images. A
    text-only model reads the action's result in words instead."""

    kind = str(message.get("type") or "")
    data = message.get("data") if isinstance(message.get("data"), Mapping) else message
    content = data.get("content")
    return (kind in {"human", "user"} and not data.get("id") and isinstance(content, list)
            and any(isinstance(part, Mapping) and part.get("type") == "image_url" for part in content))


__all__ = ["MARKER", "Missing", "is_page_picture", "join", "split"]
