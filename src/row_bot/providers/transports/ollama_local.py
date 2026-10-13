"""``ChatOllama`` for the local Ollama daemon: one runner per model, kept warm.

Ollama reloads a model's runner whenever a request asks for a different
``num_ctx`` than the loaded one (``sched.go`` ``needsReload``), and a request
without ``num_ctx`` asks for the server default. A reload throws away the
prompt cache and re-reads the whole prompt. So every request for a model
carries the ``num_ctx`` it was built with, including calls that pass their own
``options`` (compaction, titles), which would otherwise replace it.

Each request also carries ``keep_alive``: 30 minutes, so a conversation's
pauses don't unload the model and its prompt cache (the server default is
5 minutes). When ``OLLAMA_KEEP_ALIVE`` is set in Row-Bot's environment the
user has chosen a lifetime for the server, so none is sent. Switching the
chat model still unloads the previous one (``models.set_model``).
"""

from __future__ import annotations

import os
from typing import Any

from langchain_core.messages import BaseMessage
from langchain_ollama import ChatOllama
from pydantic import Field

OLLAMA_CHAT_KEEP_ALIVE = "30m"


def ollama_chat_keep_alive() -> str | None:
    """The ``keep_alive`` Row-Bot sends with chat requests, or ``None`` to defer to the server."""
    if os.environ.get("OLLAMA_KEEP_ALIVE", "").strip():
        return None
    return OLLAMA_CHAT_KEEP_ALIVE


class RowBotChatOllama(ChatOllama):
    """``ChatOllama`` that sends its ``num_ctx`` and ``keep_alive`` on every request."""

    keep_alive: int | str | None = Field(default_factory=ollama_chat_keep_alive)

    def _chat_params(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        options = kwargs.get("options")
        if isinstance(options, dict) and self.num_ctx and "num_ctx" not in options:
            kwargs["options"] = {**options, "num_ctx": self.num_ctx}
        return super()._chat_params(messages, stop, **kwargs)
