"""Stable, opaque keys for provider cache routing.

Providers route a request to a machine (or upstream provider) that may hold
its prompt cache. A stable per-conversation key keeps every call of one
conversation on the same route, so the cached prefix is found again:

- xAI Responses: ``prompt_cache_key``;
- xAI Chat Completions: the ``x-grok-conv-id`` header;
- OpenRouter: ``session_id`` (sticky provider routing);
- Codex: the ``session_id`` header;
- OpenCode: the ``x-opencode-session`` header.

OpenAI's and Codex's ``prompt_cache_key`` is shared by interactive chats
instead (``shared_prefix_cache_key``), so a new chat finds the system prompt
and tools another chat cached.

The key is an HMAC of the conversation id under a key that lives only in this
process, so a provider never sees the conversation id itself (channel
conversation ids embed chat ids and phone numbers), and the same conversation
gets an unrelated key at each provider. Calls made outside a conversation
(titles before one exists, unattended background jobs) get no key.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sys
import uuid
from typing import Any, Callable

# Lives only as long as the process: keys change on restart, which costs at
# most one cache miss per conversation and keeps nothing on disk.
_PROCESS_KEY = secrets.token_bytes(32)


def conversation_digest(conversation_id: str, *, scope: str = "") -> str:
    """32 hex characters: HMAC-SHA256 of ``scope`` and the conversation id."""
    message = f"{scope}\x00{conversation_id}" if scope else str(conversation_id or "")
    return hmac.new(_PROCESS_KEY, message.encode("utf-8"), hashlib.sha256).hexdigest()[:32]


def current_conversation_id() -> str:
    """The conversation the running agent call belongs to, or ``""``.

    The agent publishes it in a context variable that LangGraph copies into
    the threads that make model calls. Before the agent module is loaded no
    conversation exists, and importing it here would be heavy.
    """
    agent = sys.modules.get("row_bot.agent")
    getter = getattr(agent, "get_current_thread_id", None) if agent is not None else None
    if not callable(getter):
        return ""
    try:
        return str(getter() or "")
    except Exception:
        return ""


def current_runtime_surface() -> str:
    """The surface of the running agent call (``normal_chat``, ``workflow`` …), or ``""``."""
    agent = sys.modules.get("row_bot.agent")
    getter = getattr(agent, "get_current_runtime_surface", None) if agent is not None else None
    if not callable(getter):
        return ""
    try:
        return str(getter() or "")
    except Exception:
        return ""


def _uuid_key(digest: str) -> str:
    # Shaped as a version-4 UUID, the form Codex CLI and xAI's examples use.
    return str(uuid.UUID(bytes=bytes.fromhex(digest), version=4))


def conversation_cache_key(scope: str) -> str | None:
    """A UUID-shaped cache key for the current conversation at one provider.

    ``scope`` names the provider, so two providers never share a key. Returns
    ``None`` outside a conversation, where callers send no key.
    """
    conversation_id = current_conversation_id()
    if not conversation_id:
        return None
    return _uuid_key(conversation_digest(conversation_id, scope=scope))


# Back-to-back calls (workflows, goals, helpers): each keeps its own conversation's key.
_OWN_KEY_SURFACES = frozenset({"workflow", "goal", "agent", "agent_child"})


def shared_prefix_cache_key(scope: str) -> str | None:
    """The ``prompt_cache_key`` for OpenAI and Codex: shared by interactive chats.

    Every interactive chat (desktop, channels, voice) starts with the same
    system prompt and tools. OpenAI routes a key and the prompt's first tokens
    to one cache, and asks for one key across requests that share a prefix
    (under about 15 requests a minute each). With a key per conversation, each
    new chat missed the prefix the last one cached. Workflows, goals and
    helpers make back-to-back calls, so they keep their conversation's key.
    ``None`` outside a conversation.
    """
    if not current_conversation_id():
        return None
    if current_runtime_surface() in _OWN_KEY_SURFACES:
        return conversation_cache_key(scope)
    return _uuid_key(conversation_digest("interactive", scope=f"shared\x00{scope}"))


def conversation_header_hook(header: str, scope: str) -> Callable[[Any], None]:
    """An httpx request hook that sets ``header`` to the conversation's key.

    A request made outside a conversation is left without the header.
    """

    def add_header(request: Any) -> None:
        key = conversation_cache_key(scope)
        if key:
            request.headers[header] = key

    return add_header
