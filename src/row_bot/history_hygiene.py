"""Sticky, batched clearing of old tool output (context and cost plan, WP A4a).

Old tool results used to be rewritten on every model call. A rolling five-turn window masked one
more turn each turn, browser snapshots beyond the newest few became stubs one at a time, and a
repeated result replaced its earlier copy. Each rewrite changed bytes a provider had already
cached, so the conversation tail was written to the cache again and again.

Now clearing moves only in jumps. A thread keeps two marks in ``thread_meta``:

* ``mask``: a non-browser tool result before it (and over ``MASK_MIN_TOKENS``) is sent as a
  deterministic one-line summary;
* ``browser``: a browser result before it (or before ``mask``) is sent as a stub with its URL,
  title and action.

The marks advance only when the output they would clear is worth a cache rewrite
(``jump_threshold``), or when the request reaches the point where it would be compacted. They then
advance in one step: ``mask`` to the start of the latest ``KEEP_PERSON_TURNS`` person turns, and
``browser`` to the newest browser results kept in full. Between jumps every earlier result is sent
exactly as before, and a cleared result is cleared the same way on every later call.

A mark is anchored on a message (its id, its tool call id, or its digest) rather than on a bare
position, so it holds while the history only grows, and resets when the anchored message is gone
(a retry or rewind past it).

After compaction the summary boundary is the floor: results before it are no longer sent, so only
results after it count towards the next jump (``boundary`` in the agent's planning). A later
compaction work package can move the floor without changing the marks.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any, Sequence

from row_bot.turn_context import is_person_message

logger = logging.getLogger(__name__)

STATE_VERSION = 1
# The latest person turns whose tool results stay in full after a jump (the old rolling window).
KEEP_PERSON_TURNS = 5
# A non-browser result this size or smaller is never summarized.
MASK_MIN_TOKENS = 128
# A jump clears at least this share of the history budget (clamped below), so it is rare: one
# cache rewrite per batch rather than one per turn (Anthropic's ``clear_at_least``).
JUMP_SHARE = 0.20
JUMP_MIN_TOKENS = 2_000
JUMP_MAX_TOKENS = 50_000


@dataclass(frozen=True)
class Marks:
    """History positions the clearing has reached (0: nothing cleared)."""

    mask: int = 0
    browser: int = 0
    jumps: int = 0


def jump_threshold(history_budget_tokens: int) -> int:
    """Tokens of clearable tool output that justify one jump (one cache rewrite)."""
    budget = max(0, int(history_budget_tokens or 0))
    return max(JUMP_MIN_TOKENS, min(JUMP_MAX_TOKENS, int(budget * JUMP_SHARE)))


def protected_start(messages: Sequence[Any], keep: int = KEEP_PERSON_TURNS) -> int:
    """Where the latest *keep* person turns begin (0 while there are no more than *keep*)."""
    people = [index for index, message in enumerate(messages) if is_person_message(message)]
    return people[-keep] if len(people) > keep else 0


def _digest(message: Any) -> str:
    content = getattr(message, "content", "")
    encoded = json.dumps(
        [str(getattr(message, "type", "") or ""), content],
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:32]


def anchor(messages: Sequence[Any], index: int) -> dict | None:
    """A durable reference to ``messages[index]`` (None for position 0 or past the end)."""
    if index <= 0 or index >= len(messages):
        return None
    message = messages[index]
    return {
        "index": int(index),
        "id": str(getattr(message, "id", "") or ""),
        "tool_call_id": str(getattr(message, "tool_call_id", "") or ""),
        "digest": _digest(message),
    }


def _same_message(message: Any, value: dict) -> bool:
    message_id = str(value.get("id") or "")
    if message_id and str(getattr(message, "id", "") or "") == message_id:
        return True
    call_id = str(value.get("tool_call_id") or "")
    if call_id and str(getattr(message, "tool_call_id", "") or "") == call_id:
        return True
    return False


def resolve(messages: Sequence[Any], value: Any) -> int:
    """Where an anchored message is now; 0 when it is gone (the mark resets)."""
    if not isinstance(value, dict):
        return 0
    try:
        index = int(value.get("index") or 0)
    except (TypeError, ValueError):
        return 0
    if 0 < index < len(messages):
        candidate = messages[index]
        if _same_message(candidate, value) or _digest(candidate) == str(value.get("digest") or ""):
            return index
    if not (value.get("id") or value.get("tool_call_id")):
        return 0
    for position, message in enumerate(messages):
        if position and _same_message(message, value):
            return position
    return 0


def load_marks(thread_id: str, messages: Sequence[Any]) -> Marks:
    """The thread's marks resolved against its current messages."""
    if not thread_id:
        return Marks()
    try:
        from row_bot.threads import load_history_hygiene

        state = load_history_hygiene(thread_id)
    except Exception:
        logger.debug("History hygiene marks unavailable", exc_info=True)
        return Marks()
    if not state or int(state.get("version") or 0) != STATE_VERSION:
        return Marks()
    try:
        jumps = max(0, int(state.get("jumps") or 0))
    except (TypeError, ValueError):
        jumps = 0
    return Marks(
        mask=resolve(messages, state.get("mask")),
        browser=resolve(messages, state.get("browser")),
        jumps=jumps,
    )


def save_marks(thread_id: str, messages: Sequence[Any], marks: Marks) -> bool:
    """Persist *marks* for the thread, anchored on its current messages."""
    if not thread_id:
        return False
    state = {
        "version": STATE_VERSION,
        "mask": anchor(messages, marks.mask),
        "browser": anchor(messages, marks.browser),
        "jumps": int(marks.jumps),
    }
    try:
        from row_bot.threads import save_history_hygiene

        return save_history_hygiene(thread_id, state)
    except Exception:
        logger.debug("History hygiene marks were not saved", exc_info=True)
        return False
