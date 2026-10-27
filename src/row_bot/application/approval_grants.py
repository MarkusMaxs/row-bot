"""Approve the rest of a conversation's actions of one kind (F21, 5.3.1 B2).

A person answering an approval card can approve that action and every later
one of the same kind: for the rest of the turn, or for the rest of the
conversation ("Allow in this chat"). Any tool qualifies, except actions that
reach other people, cost money, delete, publish code, read Row-Bot's private
data, carry a high risk, or that only the person may approve: those always
ask each time. Grants live in memory, belong to one conversation, and end
with the turn or the conversation (or a restart).
"""

from __future__ import annotations

import re
import threading
from collections.abc import Mapping
from typing import Any, Literal

Scope = Literal["turn", "chat"]

# Commands are a kind by their tool and what they do ("run_command:run_install"); these classes always ask.
_COMMAND_TOOLS = frozenset({"developer_run_command", "run_command", "start_process"})
_ALWAYS_ASK_COMMANDS = frozenset({"delete", "git_push", "git_pr"})
# Words in a tool's name or a card's label for actions that always ask.
_ALWAYS_ASK = re.compile(
    r"send|post|repl(?:y|ies)|forward|invite|share|publish|delete|remove|erase|unlink|uninstall|"
    r"pay|purchase|buy|checkout|push|pull_request|git_pr|private|password|permission",
    re.IGNORECASE,
)

_LOCK = threading.Lock()
_GRANTS: dict[Scope, dict[str, set[str]]] = {"turn": {}, "chat": {}}


def _kind(item: Any) -> str | None:
    if not isinstance(item, Mapping):
        return None
    if item.get("person_only") or item.get("x_post") or str(item.get("risk_class") or "") in {"high", "critical"}:
        return None
    tool = str(item.get("tool") or "")
    if not tool:
        return None
    if tool in _COMMAND_TOOLS:
        from row_bot.developer.runtime import classify_command_action

        args = item.get("args") if isinstance(item.get("args"), Mapping) else {}
        action = classify_command_action(str(args.get("command") or ""))
        if action in _ALWAYS_ASK_COMMANDS or _ALWAYS_ASK.search(str(item.get("label") or "")):
            return None
        return f"{tool}:{action}"
    if _ALWAYS_ASK.search(tool) or _ALWAYS_ASK.search(str(item.get("label") or "")):
        return None
    return tool


def approval_kinds(interrupt: Any) -> frozenset[str] | None:
    """The kinds one approval covers, or None when any of its actions must always ask."""
    items = interrupt if isinstance(interrupt, list) else [interrupt]
    kinds = [_kind(item) for item in items]
    if not kinds or any(kind is None for kind in kinds):
        return None
    return frozenset(kind for kind in kinds if kind)


def grant(conversation_id: str, kinds: frozenset[str], scope: Scope = "turn") -> None:
    with _LOCK:
        _GRANTS[scope].setdefault(conversation_id, set()).update(kinds)


def granted(conversation_id: str, interrupt: Any) -> bool:
    """True when every action of this approval was approved for the rest of the turn or the chat."""
    kinds = approval_kinds(interrupt)
    if not kinds:
        return False
    with _LOCK:
        allowed = _GRANTS["turn"].get(conversation_id, set()) | _GRANTS["chat"].get(conversation_id, set())
    return kinds <= allowed


def end_turn(conversation_id: str) -> None:
    with _LOCK:
        _GRANTS["turn"].pop(conversation_id, None)


def end_chat(conversation_id: str) -> None:
    """The conversation was deleted: what it allowed goes with it."""
    with _LOCK:
        _GRANTS["turn"].pop(conversation_id, None)
        _GRANTS["chat"].pop(conversation_id, None)
