from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from langchain_core.messages import BaseMessage, SystemMessage


ANTHROPIC_CACHE_MARKER: dict[str, str] = {"type": "ephemeral"}


@dataclass(frozen=True)
class PromptCacheMarkerResult:
    provider_id: str
    applied: bool
    marker_count: int
    eligible_system_count: int
    stable_fingerprint: str = ""
    reason: str = ""


def content_has_cache_control(content: Any) -> bool:
    return isinstance(content, list) and any(
        isinstance(block, dict) and "cache_control" in block
        for block in content
    )


def _int_value(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _mapping_value(mapping: dict[str, Any], path: tuple[str, ...]) -> Any:
    current: Any = mapping
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _first_int(mapping: dict[str, Any], paths: tuple[tuple[str, ...], ...]) -> int | None:
    for path in paths:
        value = _int_value(_mapping_value(mapping, path))
        if value is not None:
            return value
    return None


def _float_value(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed == parsed and 0 <= parsed < float("inf") else None


@dataclass(frozen=True)
class ModelUsage:
    """What one model call used, with one meaning for every provider.

    ``input_tokens`` counts every prompt token, including the tokens read from
    and written to a prompt cache. ``output_tokens`` includes reasoning tokens.
    ``cost`` is only what the provider itself reported (OpenRouter). A field is
    ``None`` when the provider did not report it, which is not the same as 0.
    """

    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    cache_write_1h_tokens: int | None = None
    reasoning_tokens: int | None = None
    cost: float | None = None

    @property
    def reported(self) -> bool:
        return any(
            value is not None
            for value in (
                self.input_tokens,
                self.output_tokens,
                self.cache_read_tokens,
                self.cache_write_tokens,
                self.reasoning_tokens,
            )
        )


# Tokens read from a prompt cache. In every shape they are part of the prompt
# total: LangChain ``usage_metadata``, raw Anthropic, OpenAI Chat Completions
# and Responses, Gemini, DeepSeek, and the older aliases Row-Bot accepted.
_CACHE_READ_PATHS: tuple[tuple[str, ...], ...] = (
    ("input_token_details", "cache_read"),
    ("cache_read_input_tokens",),
    ("prompt_tokens_details", "cached_tokens"),
    ("input_tokens_details", "cached_tokens"),
    ("cached_content_token_count",),
    ("prompt_cache_hit_tokens",),
    ("prompt_cache_read_tokens",),
    ("input_cached_tokens",),
    ("cached_tokens",),
)
_CACHE_WRITE_PATHS: tuple[tuple[str, ...], ...] = (
    ("cache_creation_input_tokens",),
    ("prompt_tokens_details", "cache_write_tokens"),
    ("input_tokens_details", "cache_write_tokens"),
    ("cache_write_input_tokens",),
    ("prompt_cache_write_tokens",),
    ("input_cache_creation_tokens",),
    ("input_tokens_details", "cache_creation_tokens"),
    ("prompt_tokens_details", "cache_creation_tokens"),
)
_REASONING_PATHS: tuple[tuple[str, ...], ...] = (
    ("output_token_details", "reasoning"),
    ("completion_tokens_details", "reasoning_tokens"),
    ("output_tokens_details", "reasoning_tokens"),
    ("thoughts_token_count",),
)
# Prompt totals that never leave out cached tokens.
_PROMPT_TOTAL_PATHS: tuple[tuple[str, ...], ...] = (
    ("prompt_tokens",),
    ("prompt_token_count",),
    ("input_token_count",),
    ("prompt_eval_count",),
)
_OUTPUT_PATHS: tuple[tuple[str, ...], ...] = (
    ("output_tokens",),
    ("completion_tokens",),
    ("eval_count",),
    ("output_token_count",),
)


def _cache_write_split(details: Any) -> tuple[int | None, int | None]:
    """Cache writes from a generic ``cache_creation`` count or the 5m/1h split.

    langchain-anthropic zeroes ``cache_creation`` when it reports the split, so
    the larger reading is the write, never both added together.
    """
    if not isinstance(details, dict):
        return None, None
    generic = _int_value(details.get("cache_creation"))
    five_minutes = _int_value(details.get("ephemeral_5m_input_tokens"))
    one_hour = _int_value(details.get("ephemeral_1h_input_tokens"))
    split = None if five_minutes is None and one_hour is None else (five_minutes or 0) + (one_hour or 0)
    if generic is None and split is None:
        return None, one_hour
    return max(generic or 0, split or 0), one_hour


def _usage_from_mapping(usage: dict[str, Any]) -> ModelUsage:
    cache_read = _first_int(usage, _CACHE_READ_PATHS)
    cache_write, write_1h = _cache_write_split(usage.get("input_token_details"))
    if cache_write is None:
        cache_write = _first_int(usage, _CACHE_WRITE_PATHS)
        split_write, write_1h = _cache_write_split(usage.get("cache_creation"))
        if cache_write is None:
            cache_write = split_write
    reasoning = _first_int(usage, _REASONING_PATHS)
    output = _first_int(usage, _OUTPUT_PATHS)
    if output is None:
        candidates = _int_value(usage.get("candidates_token_count"))
        if candidates is not None:
            output = candidates + (_int_value(usage.get("thoughts_token_count")) or 0)
    prompt = _first_int(usage, _PROMPT_TOTAL_PATHS)
    if prompt is None:
        raw_input = _int_value(usage.get("input_tokens"))
        anthropic_raw = "input_token_details" not in usage and (
            "cache_read_input_tokens" in usage or "cache_creation_input_tokens" in usage
        )
        if raw_input is not None and anthropic_raw:
            # Raw Anthropic ``input_tokens`` is only the uncached remainder.
            prompt = raw_input + (cache_read or 0) + (cache_write or 0)
        else:
            prompt = raw_input
    if prompt is None:
        hit = _int_value(usage.get("prompt_cache_hit_tokens"))
        miss = _int_value(usage.get("prompt_cache_miss_tokens"))
        if hit is not None or miss is not None:
            prompt = (hit or 0) + (miss or 0)
    return ModelUsage(
        input_tokens=prompt,
        output_tokens=output,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
        cache_write_1h_tokens=write_1h,
        reasoning_tokens=reasoning,
        cost=_float_value(usage.get("cost")),
    )


def normalize_model_usage(
    response_metadata: dict[str, Any] | None,
    usage_metadata: dict[str, Any] | None = None,
) -> ModelUsage:
    """Read one call's usage from every shape a provider or transport returns.

    Sources, most standard first: LangChain ``usage_metadata`` (the argument,
    or the ``usage_metadata`` key Row-Bot merges into response metadata), then
    ``token_usage``, ``usage`` and top-level keys (Ollama counts, OpenRouter
    ``cost``). Each field comes from the first source that reports it, so a
    ``token_usage`` block never hides ``usage_metadata``.
    """
    metadata = dict(response_metadata or {})
    readings: list[ModelUsage] = []
    for candidate in (
        usage_metadata,
        metadata.get("usage_metadata"),
        metadata.get("token_usage"),
        metadata.get("usage"),
        metadata,
    ):
        if isinstance(candidate, dict) and candidate:
            readings.append(_usage_from_mapping(candidate))
    merged: dict[str, Any] = {}
    for field_name in ModelUsage.__dataclass_fields__:
        for reading in readings:
            value = getattr(reading, field_name)
            if value is not None:
                merged[field_name] = value
                break
    return ModelUsage(**merged)


def normalize_prompt_cache_usage(
    response_metadata: dict[str, Any] | None,
    usage_metadata: dict[str, Any] | None = None,
) -> dict[str, int]:
    """Return prompt-cache token counts from provider response metadata."""
    usage = normalize_model_usage(response_metadata, usage_metadata)
    normalized: dict[str, int] = {}
    if usage.cache_read_tokens is not None:
        normalized["prompt_cache_read_tokens"] = usage.cache_read_tokens
    if usage.cache_write_tokens is not None:
        normalized["prompt_cache_write_tokens"] = usage.cache_write_tokens
    return normalized


def langchain_usage_metadata(usage: ModelUsage) -> dict[str, Any] | None:
    """LangChain ``usage_metadata`` for a transport's final stream chunk."""
    if usage.input_tokens is None and usage.output_tokens is None:
        return None
    input_tokens = int(usage.input_tokens or 0)
    output_tokens = int(usage.output_tokens or 0)
    result: dict[str, Any] = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
    }
    input_details: dict[str, int] = {}
    if usage.cache_read_tokens is not None:
        input_details["cache_read"] = usage.cache_read_tokens
    if usage.cache_write_tokens is not None:
        input_details["cache_creation"] = usage.cache_write_tokens
    if input_details:
        result["input_token_details"] = input_details
    if usage.reasoning_tokens is not None:
        result["output_token_details"] = {"reasoning": usage.reasoning_tokens}
    return result


def usage_metadata_from_provider_usage(usage: Any) -> dict[str, Any] | None:
    """LangChain ``usage_metadata`` from one raw provider ``usage`` object."""
    if not isinstance(usage, dict) or not usage:
        return None
    return langchain_usage_metadata(_usage_from_mapping(usage))


# ── Prompt-cache breakpoints (WP A3) ────────────────────────────────────────
#
# Lifetimes are automatic (D3, no setting):
# - a conversation a person is in (desktop and remote client chat, voice, Designer,
#   channels and channel approvals) caches for one hour, because people pause for
#   longer than five minutes and the one-hour write premium applies only to each
#   call's small new part;
# - back-to-back loops (workflow and task runs, goal continuations, child agents,
#   compaction) cache for five minutes, which their next call always refreshes.
# Both the system breakpoint and the conversation tail use the request's lifetime,
# so the longer lifetime never follows a shorter one (an API rule).
CACHE_TTL_INTERACTIVE = "1h"
CACHE_TTL_LOOP = "5m"
_CACHE_TTL_RANK = {CACHE_TTL_LOOP: 1, CACHE_TTL_INTERACTIVE: 2}
# Runtime surfaces that are back-to-back loops; every other surface is a person's conversation.
LOOP_RUNTIME_SURFACES = frozenset({"workflow", "goal"})

# Private request metadata: set on the last message of a conversation request (Agent and
# Chat Only) to ask a Claude transport to cache the conversation tail. Transports read it from
# the LangChain messages; no request builder sends the ``additional_kwargs`` of a person or tool
# message, so it never reaches a provider.
CONVERSATION_CACHE_KEY = "row_bot_prompt_cache"


def prompt_cache_lifetime(
    *,
    runtime_surface: str = "",
    background: bool = False,
    goal_continuation: bool = False,
) -> str:
    """The automatic cache lifetime for a request (D3): ``"1h"`` or ``"5m"``."""
    surface = str(runtime_surface or "").strip().lower()
    if background or goal_continuation or surface in LOOP_RUNTIME_SURFACES or surface.startswith("agent_child"):
        return CACHE_TTL_LOOP
    return CACHE_TTL_INTERACTIVE


def normalize_cache_ttl(ttl: Any) -> str:
    """``"1h"`` or ``"5m"``; anything else is the five-minute default."""
    return CACHE_TTL_INTERACTIVE if str(ttl or "").strip().lower() == CACHE_TTL_INTERACTIVE else CACHE_TTL_LOOP


def cache_marker(ttl: str | None = None) -> dict[str, str]:
    """A ``cache_control`` value. Five minutes is the API default, so only one hour names a ``ttl``."""
    marker = dict(ANTHROPIC_CACHE_MARKER)
    if normalize_cache_ttl(ttl) == CACHE_TTL_INTERACTIVE:
        marker["ttl"] = CACHE_TTL_INTERACTIVE
    return marker


def marker_ttl(marker: Any) -> str:
    """The lifetime a ``cache_control`` value asks for."""
    return normalize_cache_ttl(marker.get("ttl") if isinstance(marker, dict) else None)


def shorter_ttl(first: str, second: str) -> str:
    """The shorter of two lifetimes: a later breakpoint may never outlive an earlier one."""
    first, second = normalize_cache_ttl(first), normalize_cache_ttl(second)
    return first if _CACHE_TTL_RANK[first] <= _CACHE_TTL_RANK[second] else second


def with_conversation_cache(messages: list[BaseMessage], *, ttl: str) -> list[BaseMessage]:
    """Ask the transport to cache this conversation request's tail for ``ttl``.

    The request is tagged on a copy of its last message (private metadata, never sent).
    """
    if not messages:
        return list(messages)
    tagged = list(messages)
    last = tagged[-1]
    additional = dict(getattr(last, "additional_kwargs", None) or {})
    additional[CONVERSATION_CACHE_KEY] = {"ttl": normalize_cache_ttl(ttl)}
    tagged[-1] = _copy_message(last, additional_kwargs=additional)
    return tagged


def conversation_cache_ttl(messages: Any) -> str | None:
    """The tail lifetime a conversation request asked for, or None for any other request."""
    if not isinstance(messages, (list, tuple)) or not messages:
        return None
    additional = getattr(messages[-1], "additional_kwargs", None)
    if not isinstance(additional, dict):
        return None
    request = additional.get(CONVERSATION_CACHE_KEY)
    if not isinstance(request, dict):
        return None
    return normalize_cache_ttl(request.get("ttl"))


def _copy_message(message: BaseMessage, **updates: Any) -> BaseMessage:
    try:
        return message.model_copy(update=updates)
    except AttributeError:
        return message.copy(update=updates)


def _marked_content(content: Any, marker: dict[str, str]) -> list[Any]:
    """Content blocks with ``marker`` on the last text block."""
    if isinstance(content, list):
        next_content = list(content)
        for index in range(len(next_content) - 1, -1, -1):
            block = next_content[index]
            if isinstance(block, dict) and str(block.get("type") or "text") == "text":
                next_content[index] = {**block, "cache_control": dict(marker)}
                return next_content
        next_content.append({"type": "text", "text": "", "cache_control": dict(marker)})
        return next_content
    return [{
        "type": "text",
        "text": content if isinstance(content, str) else str(content or ""),
        "cache_control": dict(marker),
    }]


def _with_anthropic_cache_marker(message: SystemMessage, ttl: str | None = None) -> SystemMessage:
    return _copy_message(message, content=_marked_content(message.content, cache_marker(ttl)))


def _system_blocks(content: Any) -> list[dict[str, Any]]:
    """A system message's content as text blocks (strings become one block)."""
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    blocks: list[dict[str, Any]] = []
    for block in content if isinstance(content, list) else [str(content or "")]:
        if isinstance(block, str):
            if block:
                blocks.append({"type": "text", "text": block})
        elif isinstance(block, dict) and str(block.get("type") or "text") == "text":
            blocks.append(dict(block))
    return blocks


def _consolidated_system_blocks(
    messages: list[BaseMessage],
    marked_ids: set[int],
    ttl: str | None,
) -> list[BaseMessage]:
    """One leading system message whose content keeps each system message as its own text block.

    Chat-style Claude routes (OpenRouter) take a breakpoint on one block of the system
    message, so the system prompt is no longer flattened into one string; the marked
    block is the last cache-eligible system message.
    """
    system = [message for message in messages if isinstance(message, SystemMessage)]
    if not system:
        return list(messages)
    rest = [message for message in messages if not isinstance(message, SystemMessage)]
    blocks: list[dict[str, Any]] = []
    for message in system:
        message_blocks = _system_blocks(message.content)
        if id(message) in marked_ids:
            message_blocks = _marked_content(message_blocks, cache_marker(ttl))
        blocks.extend(message_blocks)
    return [SystemMessage(content=blocks), *rest]


def apply_anthropic_system_cache_marker(
    messages: list[BaseMessage],
    *,
    provider_id: str | None,
    cache_eligible_system_message_ids: set[int],
    stable_fingerprint: str = "",
    model_id: str | None = None,
    ttl: str | None = None,
) -> tuple[list[BaseMessage], PromptCacheMarkerResult]:
    """Put one breakpoint on the last stable system block, on Claude routes that take one.

    The direct API keeps its system messages (langchain-anthropic turns them into system
    blocks). OpenRouter gets one system message of text blocks, one per system message.
    ``ttl`` is the request's lifetime (``prompt_cache_lifetime``). Other routes are unchanged.
    """
    from row_bot.providers.capabilities import (
        PROMPT_CACHE_ROUTE_OPENROUTER,
        PROMPT_CACHE_SYSTEM_BREAKPOINT_ROUTES,
        prompt_cache_route,
    )

    provider = str(provider_id or "")
    route = prompt_cache_route(provider, model_id)
    eligible_indices = [
        index
        for index, message in enumerate(messages)
        if isinstance(message, SystemMessage) and id(message) in cache_eligible_system_message_ids
    ]
    if route not in PROMPT_CACHE_SYSTEM_BREAKPOINT_ROUTES:
        return list(messages), PromptCacheMarkerResult(
            provider_id=provider,
            applied=False,
            marker_count=0,
            eligible_system_count=len(eligible_indices),
            stable_fingerprint=stable_fingerprint,
            reason="route_takes_no_breakpoints",
        )
    marked_ids = {id(messages[eligible_indices[-1]])} if eligible_indices else set()
    if route == PROMPT_CACHE_ROUTE_OPENROUTER:
        marked_messages = _consolidated_system_blocks(list(messages), marked_ids, ttl)
    else:
        marked_messages = list(messages)
        if eligible_indices:
            target_index = eligible_indices[-1]
            marked_messages[target_index] = _with_anthropic_cache_marker(marked_messages[target_index], ttl)
    return marked_messages, PromptCacheMarkerResult(
        provider_id=provider,
        applied=bool(eligible_indices),
        marker_count=1 if eligible_indices else 0,
        eligible_system_count=len(eligible_indices),
        stable_fingerprint=stable_fingerprint,
        reason="stable_system_breakpoint" if eligible_indices else "no_cache_eligible_system",
    )
