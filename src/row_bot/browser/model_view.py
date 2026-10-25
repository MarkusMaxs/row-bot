"""The picture a model that sees images gets with each browser or computer action.

A browser action's result is text (the page outline), and so is a computer
action's (the window and its controls). For a chat model that can see images,
the action also keeps a picture (the page with protected fields covered, or
the window); the model's next call gets it as one extra message right after
the action's result. The picture lives only in memory, is never saved to the
conversation, and only the latest one is sent, so earlier pictures never
cost tokens again.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
import logging
import threading
from typing import Any

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()


_BROWSER_CAPTION = "The browser page after that action (password and card fields are covered)."


@dataclass
class _Picture:
    jpeg: bytes
    caption: str = _BROWSER_CAPTION
    call: str = ""  # The tool call it was bound to, once a model call saw it.


_PICTURES: dict[str, _Picture] = {}


def model_sees_images(model: str | None = None) -> bool:
    """Whether the chat model is known to take images (unknown counts as no: text only)."""

    if model is None:
        try:
            from row_bot import models

            model = str(models._active_model_override.get() or models.get_current_model() or "")
        except Exception:
            return False
    model = str(model or "").strip()
    if not model:
        return False
    try:
        from row_bot.providers.selection import parse_model_ref

        parsed = parse_model_ref(model)
        provider, model_id = parsed if parsed else ("", model)
        if not provider or provider in {"local", "ollama"}:
            from row_bot.providers.ollama import is_ollama_vision_capable

            return bool(is_ollama_vision_capable(model_id))
        from row_bot.providers.capabilities import snapshot_supports_surface
        from row_bot.providers.capability_resolution import resolve_capability_snapshot

        snapshot = resolve_capability_snapshot(provider, model_id)
        if not snapshot or not snapshot_supports_surface(snapshot, "vision"):
            return False
        from row_bot.vision import vision_model_compatibility

        return bool(vision_model_compatibility(model).get("usable"))
    except Exception:
        logger.debug("Could not tell whether the chat model sees images", exc_info=True)
        return False


def keep(thread_id: str, jpeg: bytes | None, *, caption: str = _BROWSER_CAPTION) -> None:
    """Keep the picture after one browser or computer action (it replaces the previous one)."""

    with _LOCK:
        if jpeg:
            _PICTURES[str(thread_id)] = _Picture(bytes(jpeg), str(caption))
        else:
            _PICTURES.pop(str(thread_id), None)


def forget(thread_id: str) -> None:
    with _LOCK:
        _PICTURES.pop(str(thread_id), None)


def with_page_picture(messages: list[Any], thread_id: str, *, model: str | None = None) -> list[Any]:
    """*messages* with the latest picture after the browser or computer action that took it.

    Only when the last message is that action's result (a retried call gets it
    again) and the model sees images; otherwise *messages* unchanged.
    """

    if not messages:
        return messages
    from langchain_core.messages import HumanMessage, ToolMessage

    last = messages[-1]
    if not isinstance(last, ToolMessage):
        return messages
    call = str(getattr(last, "tool_call_id", "") or "")
    with _LOCK:
        picture = _PICTURES.get(str(thread_id))
        if picture is None or not call:
            return messages
        if not picture.call:
            picture.call = call
        if picture.call != call:
            return messages
        jpeg, caption = picture.jpeg, picture.caption
    if not model_sees_images(model):
        return messages
    encoded = base64.b64encode(jpeg).decode("ascii")
    return [*messages, HumanMessage(content=[
        {"type": "text", "text": caption},
        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{encoded}"}},
    ])]
