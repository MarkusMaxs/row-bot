"""One stable, opaque key per conversation, for provider cache routing.

Providers route a request to a machine (or upstream provider) that may hold
its prompt cache. A stable per-conversation key keeps every call of one
conversation on the same route, so the cached prefix is found again:

- OpenAI and xAI Responses: ``prompt_cache_key``;
- xAI Chat Completions: the ``x-grok-conv-id`` header;
- OpenRouter: ``session_id`` (sticky provider routing);
- Codex: ``prompt_cache_key`` and the ``session_id`` header;
- OpenCode: the ``x-opencode-session`` header.

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


def conversation_cache_key(scope: str) -> str | None:
    """A UUID-shaped cache key for the current conversation at one provider.

    ``scope`` names the provider, so two providers never share a key. Returns
    ``None`` outside a conversation, where callers send no key.
    """
    conversation_id = current_conversation_id()
    if not conversation_id:
        return None
    # Shaped as a version-4 UUID, the form Codex CLI and xAI's examples use.
    return str(uuid.UUID(bytes=bytes.fromhex(conversation_digest(conversation_id, scope=scope)), version=4))


def conversation_header_hook(header: str, scope: str) -> Callable[[Any], None]:
    """An httpx request hook that sets ``header`` to the conversation's key.

    A request made outside a conversation is left without the header.
    """

    def add_header(request: Any) -> None:
        key = conversation_cache_key(scope)
        if key:
            request.headers[header] = key

    return add_header
