"""What an action will do, for a card only the person can approve ("Always ask me", N9).

The card says it exactly: the action, where it goes, the account it goes out as,
the full text, and each file it sends by name, size and a short fingerprint of
its contents. The fingerprints are part of what the approval is bound to, so a
file changed after the person approved it asks again.
"""

from __future__ import annotations

import hashlib
import mimetypes
import pathlib
from collections.abc import Mapping
from typing import Any

# Argument names, most telling first, for where an action goes and what it says.
_DESTINATION_KEYS = ("to", "recipient", "recipients", "email", "address", "chat_id", "channel", "target",
                     "destination", "repo", "repository", "url", "page_id", "parent", "path", "file_path")
_TEXT_KEYS = ("text", "message", "body", "content", "caption", "tweet", "post", "comment", "status",
              "description", "subject", "title", "command")
_MEDIA_KEYS = ("media", "media_paths", "media_path", "attachments", "attachment", "files", "file",
               "image", "image_path", "images", "video", "video_path", "audio", "audio_path")
_MAX_TEXT = 16384
_MAX_MEDIA = 16
_FINGERPRINT_LIMIT = 512 * 1024 * 1024  # Larger files are fingerprinted by their first 512 MB and size.


def _clean(value: object, limit: int) -> str:
    text = value if isinstance(value, str) else ""
    return "".join(c for c in text if c in "\n\t" or ord(c) >= 32).strip()[:limit]


def _destination(args: Mapping[str, Any]) -> str:
    for key in _DESTINATION_KEYS:
        value = args.get(key)
        if isinstance(value, (list, tuple)):
            value = ", ".join(str(item) for item in value if str(item).strip())
        text = _clean(value if isinstance(value, str) else (str(value) if isinstance(value, int) else ""), 1024)
        if text:
            return text
    return ""


def _text(args: Mapping[str, Any]) -> str:
    """The full text the action sends: the longest of its text arguments."""
    texts = [_clean(args.get(key), _MAX_TEXT) for key in _TEXT_KEYS]
    return max(texts, key=len, default="")


def _kind(path: pathlib.Path) -> str:
    kind = (mimetypes.guess_type(path.name)[0] or "").split("/", 1)[0]
    return kind if kind in {"image", "video", "audio"} else "file"


def fingerprint(path: pathlib.Path) -> str:
    """A short fingerprint of a file's contents (the first 12 hex digits of its SHA-256)."""
    digest = hashlib.sha256()
    remaining = _FINGERPRINT_LIMIT
    with path.open("rb") as handle:
        while remaining > 0 and (chunk := handle.read(min(1024 * 1024, remaining))):
            digest.update(chunk)
            remaining -= len(chunk)
    digest.update(str(path.stat().st_size).encode())
    return digest.hexdigest()[:12]


def _media(args: Mapping[str, Any]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for key in _MEDIA_KEYS:
        value = args.get(key)
        values = value if isinstance(value, (list, tuple)) else [value]
        for item in values:
            if len(found) >= _MAX_MEDIA or not isinstance(item, str) or not item.strip() or len(item) > 4096:
                continue
            try:
                path = pathlib.Path(item.strip()).expanduser()
                if not path.is_file():
                    continue
                found.append({"name": path.name[:256], "size": path.stat().st_size,
                              "fingerprint": fingerprint(path), "kind": _kind(path)})
            except (OSError, ValueError):
                continue
    return found


def _account(name: str, *, source: str, parent: str) -> str:
    """The account the action goes out as: the app it belongs to, as Apps names it."""
    try:
        from row_bot.integrations.scope import app_for_tool

        app = app_for_tool(name)
    except Exception:
        app = None
    if app and app.get("name"):
        return str(app["name"])[:256]
    if source.startswith("channel:"):
        return source.split(":", 1)[1][:256]
    return ""


def describe_action(name: str, arguments: Mapping[str, Any] | None, *, source: str = "", parent: str = "") -> dict:
    """{action, destination, account, text, media} for a person-only approval card."""
    from row_bot.approval_messages import plain_tool_call

    args = dict(arguments or {})
    return {
        "action": plain_tool_call(name, {}, max_chars=200) or "Run a tool",
        "destination": _destination(args),
        "account": _account(name, source=source, parent=parent),
        "text": _text(args),
        "media": _media(args),
    }


def describe_delivery(channel: str, target: str, text: str, *, account: str = "") -> dict:
    """The card for a workflow step that sends a message to a channel."""
    return {
        "action": "Send a message",
        "destination": _clean(f"{channel}: {target}" if target else channel, 1024),
        "account": _clean(account, 256),
        "text": _clean(text, _MAX_TEXT),
        "media": [],
    }
