"""MCP CallToolResult normalization."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RawCallContent:
    """Lossless, internal-only MCP result content."""

    kind: str
    text: str = ""
    data: str = ""
    mime_type: str = ""


@dataclass(frozen=True)
class RawCallResult:
    content: tuple[RawCallContent, ...]
    structured_content: Any = None
    is_error: bool = False


def raw_call_result(result: Any) -> RawCallResult:
    """Extract text/image blocks for a reviewed internal client.

    Generic model-facing MCP results continue through ``normalize_call_result``
    and still omit images.
    """

    blocks: list[RawCallContent] = []
    for block in getattr(result, "content", None) or []:
        kind = str(getattr(block, "type", "") or "text")
        blocks.append(RawCallContent(
            kind=kind,
            text=str(getattr(block, "text", "") or ""),
            data=str(getattr(block, "data", "") or ""),
            mime_type=str(
                getattr(block, "mimeType", "")
                or getattr(block, "mime_type", "")
                or ""
            ),
        ))
    structured = getattr(result, "structuredContent", None)
    if structured is None:
        structured = getattr(result, "structured_content", None)
    return RawCallResult(tuple(blocks), structured, bool(getattr(result, "isError", False)))


def _content_block_to_text(block: Any) -> str:
    text = getattr(block, "text", None)
    if text is not None:
        return str(text)
    block_type = getattr(block, "type", None)
    if block_type == "image" or hasattr(block, "mimeType"):
        mime = getattr(block, "mimeType", "") or "binary"
        return f"[MCP {block_type or 'content'} omitted: {mime}]"
    resource = getattr(block, "resource", None)
    if resource is not None:
        uri = getattr(resource, "uri", "resource")
        resource_text = getattr(resource, "text", None)
        if resource_text is not None:
            return f"[Embedded resource: {uri}]\n{resource_text}"
        return f"[Embedded resource omitted: {uri}]"
    uri = getattr(block, "uri", None)
    if uri is not None:
        return f"[MCP resource link: {uri}]"
    return str(block)


# Said instead of repeating the structured content when the text already is it. An app's view
# rebuilds structuredContent from the text then (integrations/views.py).
STRUCTURED_SAME_AS_TEXT = "STRUCTURED_CONTENT: same as the text above"
# Leads a result the server marked isError; the model and an app's view both read it as the error flag.
ERROR_PREFIX = "MCP tool error: "


def _text_carries(parts: list[str], structured: Any) -> bool:
    """Whether the text is the structured content serialized (the MCP spec asks servers to send it
    in a text block too), so appending it again would only repeat it."""
    text = "\n\n".join(part for part in parts if part).strip()
    if not text:
        return False
    try:
        return json.loads(text) == structured
    except (TypeError, ValueError):
        return False


def normalize_call_result(result: Any, *, output_limit: int = 24000) -> str:
    """Convert an MCP call result into model-facing text."""
    parts: list[str] = []
    for block in getattr(result, "content", None) or []:
        parts.append(_content_block_to_text(block))
    structured = getattr(result, "structuredContent", None)
    if structured is not None and _text_carries(parts, structured):
        parts.append(STRUCTURED_SAME_AS_TEXT)
    elif structured is not None:
        try:
            parts.append(
                "STRUCTURED_CONTENT:\n"
                + json.dumps(structured, ensure_ascii=False, separators=(",", ":"), default=str)
            )
        except Exception:
            parts.append(f"STRUCTURED_CONTENT:\n{structured!r}")
    text = "\n\n".join(part for part in parts if part).strip()
    if getattr(result, "isError", False):
        text = ERROR_PREFIX + (text or "tool returned an error")
    if not text:
        text = "MCP tool completed with no content."
    if output_limit > 0 and len(text) > output_limit:
        text = text[:output_limit] + f"\n\n[Truncated MCP output at {output_limit} characters]"
    return text
