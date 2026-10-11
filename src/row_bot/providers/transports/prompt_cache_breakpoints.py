"""Prompt-cache breakpoints on the wire for Claude routes (WP A3).

Row-Bot's conversation paths (Agent and Chat Only) mark the last stable system
block and tag the request with its cache lifetime (``row_bot.prompt_cache``).
The Claude transports finish the job here:

- **Direct Claude API** (``place_anthropic_breakpoints``). Top-level automatic
  caching covers the conversation tail: the API puts that breakpoint on the
  last cacheable block and moves it forward as the conversation grows. When a
  request adds more than ``INTERMEDIATE_BREAKPOINT_GAP`` positions since the
  previous breakpoint Row-Bot sent with the same prefix, one explicit
  breakpoint goes where that request ended. Each breakpoint looks back at most
  20 positions for an earlier cache entry, so without it a long single turn
  would rewrite the whole conversation. A run of consecutive ``tool_use``
  blocks counts as one position, as does a run of ``tool_result`` blocks. A
  request never carries more than four breakpoints, and a later breakpoint
  never outlives an earlier one (a one-hour entry after a five-minute one is a
  400).
- **OpenRouter** takes the same top-level ``cache_control`` field
  (``with_openrouter_tail``).
- **Requesty** takes its own ``"requesty": {"auto_cache": true}`` flag
  (``with_requesty_auto_cache``).

**Safety net.** A 400 that names ``cache_control``, ``ttl`` or ``auto_cache``
on a request that carried cache instructions is retried once without them
(``without_cache_markers``). Gateways are then remembered for the process
(``remember_cache_markers_rejected``), so a gateway that rejects them costs one
failed request per process. No other 400 is retried.

This module only shapes payloads and classifies errors: no network access,
no heavy imports.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from typing import Any

from row_bot.prompt_cache import cache_marker, marker_ttl, shorter_ttl

logger = logging.getLogger(__name__)

MAX_BREAKPOINTS = 4
LOOKBACK_POSITIONS = 20
# Leave a margin inside the 20-position lookback.
INTERMEDIATE_BREAKPOINT_GAP = 15
# Content blocks a breakpoint may sit on. Thinking blocks and server-tool blocks may not.
_MARKABLE_BLOCK_TYPES = frozenset({"text", "image", "document", "tool_use", "tool_result", "search_result"})
# A run of consecutive blocks of one of these types is one lookback position.
_RUN_BLOCK_TYPES = frozenset({"tool_use", "tool_result"})

# Prefix digests of where earlier requests put their tail breakpoint, newest last.
_BREAKPOINT_DIGESTS: OrderedDict[str, None] = OrderedDict()
_MAX_REMEMBERED_BREAKPOINTS = 4096
_DIGESTS_LOCK = threading.Lock()

# Gateway routes (provider, model) that rejected cache instructions this process.
_REJECTED_ROUTES: set[tuple[str, str]] = set()
_REJECTED_LOCK = threading.Lock()

_CACHE_ERROR = re.compile(r"cache_control|\bttl\b|auto_cache", re.IGNORECASE)


# ── Direct Claude API ───────────────────────────────────────────────────────


def place_anthropic_breakpoints(payload: Mapping[str, Any], *, ttl: str) -> dict[str, Any]:
    """Top-level automatic caching for the tail, plus one breakpoint for long appends.

    ``ttl`` is the lifetime the conversation asked for. The tail never outlives a
    breakpoint before it (the system breakpoint, an intermediate one).
    """
    result = dict(payload)
    messages = _messages_without_markers(result.get("messages"))
    earlier_ttls = _system_marker_ttls(result.get("system")) + _tool_marker_ttls(result.get("tools"))
    tail_ttl = ttl
    for earlier in earlier_ttls:
        tail_ttl = shorter_ttl(tail_ttl, earlier)
    explicit = len(earlier_ttls)

    positions = _positions(messages)
    digests = _prefix_digests(result, messages, positions)
    previous = _latest_remembered(digests)
    if (
        previous is not None
        and len(positions) - 1 - previous > INTERMEDIATE_BREAKPOINT_GAP
        and explicit + 2 <= MAX_BREAKPOINTS
        and _mark_near(messages, positions, previous, tail_ttl)
    ):
        explicit += 1
    if explicit < MAX_BREAKPOINTS and positions:
        result.setdefault("cache_control", cache_marker(tail_ttl))
    result["messages"] = messages
    if digests:
        _remember(digests[-1])
    return result


def _messages_without_markers(messages: Any) -> list[Any]:
    """Message dicts without block-level ``cache_control``; only changed messages are copied."""
    cleaned: list[Any] = []
    for message in messages if isinstance(messages, list) else []:
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, list) and any(_block_has_marker(block) for block in content):
            message = {**message, "content": [_block_without_marker(block) for block in content]}
        cleaned.append(message)
    return cleaned


def _block_has_marker(block: Any) -> bool:
    if not isinstance(block, dict):
        return False
    if "cache_control" in block:
        return True
    nested = block.get("content") if block.get("type") == "tool_result" else None
    return isinstance(nested, list) and any(isinstance(item, dict) and "cache_control" in item for item in nested)


def _block_without_marker(block: Any) -> Any:
    if not _block_has_marker(block):
        return block
    cleaned = {key: value for key, value in block.items() if key != "cache_control"}
    nested = cleaned.get("content") if cleaned.get("type") == "tool_result" else None
    if isinstance(nested, list):
        cleaned["content"] = [
            {key: value for key, value in item.items() if key != "cache_control"} if isinstance(item, dict) else item
            for item in nested
        ]
    return cleaned


def _system_marker_ttls(system: Any) -> list[str]:
    if not isinstance(system, list):
        return []
    return [marker_ttl(block["cache_control"]) for block in system
            if isinstance(block, dict) and isinstance(block.get("cache_control"), dict)]


def _tool_marker_ttls(tools: Any) -> list[str]:
    if not isinstance(tools, list):
        return []
    return [marker_ttl(tool["cache_control"]) for tool in tools
            if isinstance(tool, dict) and isinstance(tool.get("cache_control"), dict)]


def _positions(messages: list[Any]) -> list[list[tuple[int, int | None]]]:
    """Lookback positions: each block, with tool_use runs and tool_result runs counted once.

    A position is a list of ``(message index, block index)``; a string content is block ``None``.
    """
    positions: list[list[tuple[int, int | None]]] = []
    previous_type = ""
    for message_index, message in enumerate(messages):
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, str):
            items: list[tuple[int | None, str]] = [(None, "text")]
        elif isinstance(content, list):
            items = [
                (block_index, str(block.get("type") or "") if isinstance(block, dict) else "text")
                for block_index, block in enumerate(content)
            ]
        else:
            items = []
        for block_index, block_type in items:
            if block_type in _RUN_BLOCK_TYPES and block_type == previous_type and positions:
                positions[-1].append((message_index, block_index))
            else:
                positions.append([(message_index, block_index)])
            previous_type = block_type
    return positions


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str).encode("utf-8")


def _canonical_block(messages: list[Any], message_index: int, block_index: int | None) -> Any:
    message = messages[message_index]
    content = message.get("content")
    block = {"type": "text", "text": content} if block_index is None else content[block_index]
    return {"role": message.get("role"), "block": _block_without_marker(block)}


def _prefix_digests(
    payload: Mapping[str, Any],
    messages: list[Any],
    positions: list[list[tuple[int, int | None]]],
) -> list[str]:
    """One digest per position, of everything up to and including it (markers ignored)."""
    seed = hashlib.sha256(_canonical({
        "model": payload.get("model"),
        "system": _without_system_markers(payload.get("system")),
        "tools": _without_tool_markers(payload.get("tools")),
    })).digest()
    digests: list[str] = []
    running = seed
    for position in positions:
        hasher = hashlib.sha256(running)
        for message_index, block_index in position:
            hasher.update(_canonical(_canonical_block(messages, message_index, block_index)))
        running = hasher.digest()
        digests.append(running.hex())
    return digests


def _without_system_markers(system: Any) -> Any:
    if not isinstance(system, list):
        return system
    return [_block_without_marker(block) for block in system]


def _without_tool_markers(tools: Any) -> Any:
    if not isinstance(tools, list):
        return tools
    return [
        {key: value for key, value in tool.items() if key != "cache_control"} if isinstance(tool, dict) else tool
        for tool in tools
    ]


def _latest_remembered(digests: list[str]) -> int | None:
    with _DIGESTS_LOCK:
        for index in range(len(digests) - 1, -1, -1):
            if digests[index] in _BREAKPOINT_DIGESTS:
                return index
    return None


def _remember(digest: str) -> None:
    with _DIGESTS_LOCK:
        _BREAKPOINT_DIGESTS[digest] = None
        _BREAKPOINT_DIGESTS.move_to_end(digest)
        while len(_BREAKPOINT_DIGESTS) > _MAX_REMEMBERED_BREAKPOINTS:
            _BREAKPOINT_DIGESTS.popitem(last=False)


def _mark_near(
    messages: list[Any],
    positions: list[list[tuple[int, int | None]]],
    previous: int,
    ttl: str,
) -> bool:
    """Put a breakpoint where the previous request ended, or just after it.

    A block in a content list is marked as it is. A string content (a person's
    message) is only rewritten as one text block when nothing within reach can be
    marked instead. The tail itself is never marked: automatic caching covers it.
    """
    last_candidate = min(len(positions) - 2, previous + LOOKBACK_POSITIONS - 2)
    for index in range(previous, last_candidate + 1):
        message_index, block_index = positions[index][-1]
        if block_index is not None and _mark_block(messages, message_index, block_index, ttl):
            return True
    message_index, block_index = positions[previous][-1]
    if block_index is None and previous <= last_candidate:
        message = messages[message_index]
        messages[message_index] = {
            **message,
            "content": [{"type": "text", "text": message.get("content"), "cache_control": cache_marker(ttl)}],
        }
        return True
    return False


def _mark_block(messages: list[Any], message_index: int, block_index: int, ttl: str) -> bool:
    message = messages[message_index]
    content = message.get("content")
    block = content[block_index] if isinstance(content, list) else None
    if not isinstance(block, dict) or str(block.get("type") or "") not in _MARKABLE_BLOCK_TYPES:
        return False
    if block.get("type") == "text" and not block.get("text"):
        return False
    next_content = list(content)
    next_content[block_index] = {**block, "cache_control": cache_marker(ttl)}
    messages[message_index] = {**message, "content": next_content}
    return True


# ── Gateways ────────────────────────────────────────────────────────────────


def with_openrouter_tail(params: Mapping[str, Any], message_dicts: Sequence[Any], *, ttl: str) -> dict[str, Any]:
    """OpenRouter's documented top-level ``cache_control``, never outliving the system breakpoint."""
    tail_ttl = ttl
    for message in message_dicts:
        if not isinstance(message, dict) or message.get("role") != "system":
            continue
        for earlier in _system_marker_ttls(message.get("content")):
            tail_ttl = shorter_ttl(tail_ttl, earlier)
    result = dict(params)
    result.setdefault("cache_control", cache_marker(tail_ttl))
    return result


def with_requesty_auto_cache(body: Mapping[str, Any]) -> dict[str, Any]:
    """Requesty's documented ``"requesty": {"auto_cache": true}`` flag."""
    result = dict(body)
    existing = result.get("requesty")
    result["requesty"] = {**(existing if isinstance(existing, dict) else {}), "auto_cache": True}
    return result


# ── Safety net ──────────────────────────────────────────────────────────────


def has_cache_markers(payload: Mapping[str, Any]) -> bool:
    """Whether a request body or message list carries any cache instruction."""
    if "cache_control" in payload:
        return True
    requesty = payload.get("requesty")
    if isinstance(requesty, dict) and "auto_cache" in requesty:
        return True
    for key in ("system", "tools"):
        value = payload.get(key)
        if isinstance(value, list) and any(_block_has_marker(item) for item in value):
            return True
    return any(
        isinstance(message, dict)
        and isinstance(message.get("content"), list)
        and any(_block_has_marker(block) for block in message["content"])
        for message in payload.get("messages") or []
    )


def without_cache_markers(payload: Mapping[str, Any]) -> dict[str, Any]:
    """The same request without any cache instruction (top-level, block-level or Requesty's flag)."""
    result = {key: value for key, value in payload.items() if key != "cache_control"}
    requesty = result.get("requesty")
    if isinstance(requesty, dict) and "auto_cache" in requesty:
        rest = {key: value for key, value in requesty.items() if key != "auto_cache"}
        if rest:
            result["requesty"] = rest
        else:
            result.pop("requesty")
    if isinstance(result.get("system"), list):
        result["system"] = [_block_without_marker(block) for block in result["system"]]
    if isinstance(result.get("tools"), list):
        result["tools"] = _without_tool_markers(result["tools"])
    if isinstance(result.get("messages"), list):
        result["messages"] = _messages_without_markers(result["messages"])
    return result


def is_cache_control_rejection(exc: BaseException) -> bool:
    """A 400 that names ``cache_control``, ``ttl`` or ``auto_cache``."""
    text = _error_text(exc)
    if _status_code(exc) != 400 and "HTTP 400" not in text:
        return False
    return bool(_CACHE_ERROR.search(text))


def cache_markers_rejected(route: tuple[str, str]) -> bool:
    with _REJECTED_LOCK:
        return route in _REJECTED_ROUTES


def remember_cache_markers_rejected(route: tuple[str, str]) -> None:
    with _REJECTED_LOCK:
        _REJECTED_ROUTES.add(route)


def log_cache_retry(route: tuple[str, str], *, remembered: bool) -> None:
    logger.warning(
        "prompt_cache: %s rejected the cache instructions for model=%s; retrying once without them%s",
        route[0],
        route[1],
        " and sending none on this route until Row-Bot restarts" if remembered else "",
    )


def _status_code(exc: BaseException) -> int:
    for candidate in (
        getattr(exc, "status_code", None),
        getattr(getattr(exc, "response", None), "status_code", None),
    ):
        try:
            status = int(candidate or 0)
        except (TypeError, ValueError):
            status = 0
        if status:
            return status
    return 0


def _error_text(exc: BaseException) -> str:
    parts = [str(exc), str(getattr(exc, "message", "") or "")]
    body = getattr(exc, "body", None)
    if isinstance(body, Mapping):
        error = body.get("error")
        if isinstance(error, Mapping):
            parts.append(str(error.get("message") or ""))
        parts.append(str(body.get("message") or ""))
    elif isinstance(body, str):
        parts.append(body)
    return " ".join(part for part in parts if part)
