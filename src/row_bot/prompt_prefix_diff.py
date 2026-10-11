"""DEBUG-only diff of consecutive provider requests in one conversation.

A prompt cache only helps when each provider request starts with the exact
bytes of the one before it. When logging for this module is at DEBUG, every
Agent and Chat Only request is fingerprinted: each system section by its
section id, the bound tools block, and each message. The fingerprints are
compared with the previous request in the same thread, and one DEBUG line
names the first diverging item and the sections that changed.

Only section ids, roles, indices and truncated sha256 digests are logged, never
prompt text. Nothing is hashed unless DEBUG is enabled for this logger, so it
costs nothing in normal use. To turn it on in a development session:
``logging.getLogger("row_bot.prompt_prefix_diff").setLevel(logging.DEBUG)``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

logger = logging.getLogger(__name__)

MAX_TRACKED_THREADS = 64
_DIGEST_CHARS = 16


@dataclass(frozen=True)
class RequestFingerprint:
    """The digests of one provider request: ordered items plus named system sections."""

    items: tuple[tuple[str, str], ...]
    sections: tuple[tuple[str, str], ...]
    message_count: int


def enabled() -> bool:
    """Whether the prefix diff runs: only when this module's logger is at DEBUG."""
    return logger.isEnabledFor(logging.DEBUG)


def _digest(data: str) -> str:
    return hashlib.sha256(data.encode("utf-8", "surrogatepass")).hexdigest()[:_DIGEST_CHARS]


def _without_cache_control(value: Any) -> Any:
    # Cache breakpoints move by design; the diff is about the content they mark.
    if isinstance(value, dict):
        return {key: _without_cache_control(item) for key, item in value.items() if key != "cache_control"}
    if isinstance(value, (list, tuple)):
        return [_without_cache_control(item) for item in value]
    return value


def _json_digest(value: Any) -> str:
    return _digest(json.dumps(
        _without_cache_control(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ))


def text_digest(text: str) -> str:
    """Digest of a system section's text, matching the digest of its SystemMessage text."""
    return _digest(str(text or ""))


def message_text(message: Any) -> str:
    """The text of a message's content, ignoring non-text blocks."""
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type", "text") == "text":
                parts.append(str(block.get("text") or ""))
        return "\n".join(parts)
    return str(content or "")


def _message_payload(message: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "role": str(getattr(message, "type", type(message).__name__)),
        "content": getattr(message, "content", ""),
    }
    tool_calls = getattr(message, "tool_calls", None)
    if tool_calls:
        payload["tool_calls"] = list(tool_calls)
    for key in ("tool_call_id", "name"):
        value = getattr(message, key, None)
        if value:
            payload[key] = str(value)
    return payload


def fingerprint_request(
    messages: Sequence[Any],
    tools: Iterable[Any],
    *,
    sections: Sequence[tuple[str, str]] = (),
) -> RequestFingerprint:
    """Fingerprint one provider request.

    ``sections`` are the (section id, text) pairs the system prompt was built
    from; a system message whose text matches one is labelled with its id.
    """
    section_digests = tuple((str(section_id), text_digest(text)) for section_id, text in sections)
    labels = {digest: section_id for section_id, digest in section_digests}
    items: list[tuple[str, str]] = [("tools", _json_digest(list(tools or ())))]
    for message in messages:
        role = str(getattr(message, "type", type(message).__name__))
        if role == "system":
            # A merged system prompt (custom endpoints, Anthropic-compatible routes) matches no one
            # section; the changed-sections list still names what moved.
            label = f"system:{labels.get(text_digest(message_text(message)), 'other')}"
        else:
            label = role
        items.append((label, _json_digest(_message_payload(message))))
    return RequestFingerprint(items=tuple(items), sections=section_digests, message_count=len(messages))


def describe_change(previous: RequestFingerprint | None, current: RequestFingerprint) -> str:
    """One line that names the first diverging item and the changed sections, without prompt text."""
    sections = ", ".join(f"{section_id}={digest}" for section_id, digest in current.sections)
    if previous is None:
        return (
            f"first request: items={len(current.items)} messages={current.message_count} "
            f"tools={current.items[0][1]} sections=[{sections}]"
        )
    diverged_at = next(
        (
            index
            for index, (before, after) in enumerate(zip(previous.items, current.items))
            if before != after
        ),
        None,
    )
    if diverged_at is None and len(current.items) >= len(previous.items):
        return (
            f"stable prefix: reused {len(previous.items)} of {len(current.items)} items, "
            f"+{len(current.items) - len(previous.items)} appended"
        )
    before_sections = dict(previous.sections)
    after_sections = dict(current.sections)
    changed = sorted(
        section_id
        for section_id in set(before_sections) | set(after_sections)
        if before_sections.get(section_id) != after_sections.get(section_id)
    )
    if diverged_at is None:
        return (
            f"prefix broken: request shrank from {len(previous.items)} to {len(current.items)} items; "
            f"changed sections=[{', '.join(changed)}]"
        )
    before_label, before_digest = previous.items[diverged_at]
    after_label, after_digest = current.items[diverged_at]
    where = "tools" if diverged_at == 0 else f"message {diverged_at - 1}"
    return (
        f"prefix broken at {where}: previous={before_label}:{before_digest} "
        f"current={after_label}:{after_digest}; reused {diverged_at} of {len(previous.items)} items; "
        f"changed sections=[{', '.join(changed)}]; sections=[{sections}]"
    )


class PrefixTracker:
    """The previous request's fingerprint per thread, bounded to the most recent threads."""

    def __init__(self, max_threads: int = MAX_TRACKED_THREADS) -> None:
        self._max_threads = max(1, int(max_threads))
        self._previous: OrderedDict[str, RequestFingerprint] = OrderedDict()
        self._lock = threading.Lock()

    def observe(self, thread_id: str, current: RequestFingerprint) -> str:
        with self._lock:
            previous = self._previous.pop(thread_id, None)
            self._previous[thread_id] = current
            while len(self._previous) > self._max_threads:
                self._previous.popitem(last=False)
        return describe_change(previous, current)

    def __len__(self) -> int:
        with self._lock:
            return len(self._previous)


_TRACKER = PrefixTracker()


def note_request(
    thread_id: str,
    messages: Sequence[Any],
    tools: Iterable[Any],
    *,
    sections: Sequence[tuple[str, str]] = (),
) -> None:
    """Log, at DEBUG, how this request's prefix compares with the thread's previous one."""
    if not thread_id or not enabled():
        return
    try:
        current = fingerprint_request(messages, tools, sections=sections)
        logger.debug("prompt prefix thread=%s %s", thread_id, _TRACKER.observe(thread_id, current))
    except Exception:
        logger.debug("prompt prefix diff skipped", exc_info=True)
