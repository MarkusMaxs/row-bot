"""Why a model stopped, read the same way from every provider's response shape (F17)."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

# Stop reasons that mean the reply hit the output token limit.
OUTPUT_LIMIT_STOP_REASONS = frozenset({
    "length",  # Chat Completions, OpenAI-compatible routes, Ollama
    "max_tokens",  # Anthropic; Gemini's MAX_TOKENS
    "max_output_tokens",  # Responses APIs (OpenAI, Codex, xAI)
})


def _reason_text(value: Any) -> str:
    # Gemini reports an enum's name; an SDK object may still arrive with its .name.
    name = getattr(value, "name", None)
    if isinstance(name, str):
        value = name
    return value.strip().lower() if isinstance(value, str) else ""


def stop_reason(response_metadata: Mapping[str, Any] | None) -> str:
    """The stop reason one response (or final chunk) reports, lower-cased, or "".

    - Anthropic: ``stop_reason`` ("end_turn", "max_tokens", "tool_use").
    - Ollama: ``done_reason`` ("stop", "length").
    - Chat Completions and OpenAI-compatible routes: ``finish_reason``
      ("stop", "length", "tool_calls"); Gemini also uses ``finish_reason``,
      with enum names ("STOP", "MAX_TOKENS").
    - Responses APIs (OpenAI, Codex, xAI) give no stop reason: ``status`` is
      "incomplete" and ``incomplete_details.reason`` says why
      ("max_output_tokens", "content_filter").
    """
    if not isinstance(response_metadata, Mapping):
        return ""
    for key in ("stop_reason", "done_reason", "finish_reason"):
        reason = _reason_text(response_metadata.get(key))
        if reason:
            return reason
    if _reason_text(response_metadata.get("status")) == "incomplete":
        details = response_metadata.get("incomplete_details")
        reason = _reason_text(details.get("reason")) if isinstance(details, Mapping) else ""
        return reason or "incomplete"
    return ""


def stopped_at_output_limit(reason: str | None) -> bool:
    """Whether a stop reason (as ``stop_reason`` returns it) means the output limit cut the reply."""
    return (reason or "") in OUTPUT_LIMIT_STOP_REASONS
