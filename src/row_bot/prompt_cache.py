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


def _with_anthropic_cache_marker(message: SystemMessage) -> SystemMessage:
    content = message.content
    if isinstance(content, str):
        next_content: Any = [
            {
                "type": "text",
                "text": content,
                "cache_control": dict(ANTHROPIC_CACHE_MARKER),
            }
        ]
    elif isinstance(content, list):
        next_content = list(content)
        for index in range(len(next_content) - 1, -1, -1):
            block = next_content[index]
            if isinstance(block, dict) and str(block.get("type") or "text") == "text":
                next_content[index] = {
                    **block,
                    "cache_control": dict(ANTHROPIC_CACHE_MARKER),
                }
                break
        else:
            next_content.append({
                "type": "text",
                "text": "",
                "cache_control": dict(ANTHROPIC_CACHE_MARKER),
            })
    else:
        next_content = [
            {
                "type": "text",
                "text": str(content or ""),
                "cache_control": dict(ANTHROPIC_CACHE_MARKER),
            }
        ]

    try:
        return message.model_copy(update={"content": next_content})
    except AttributeError:
        return message.copy(update={"content": next_content})


def apply_anthropic_system_cache_marker(
    messages: list[BaseMessage],
    *,
    provider_id: str | None,
    cache_eligible_system_message_ids: set[int],
    stable_fingerprint: str = "",
) -> tuple[list[BaseMessage], PromptCacheMarkerResult]:
    provider = str(provider_id or "")
    eligible_indices = [
        index
        for index, message in enumerate(messages)
        if isinstance(message, SystemMessage) and id(message) in cache_eligible_system_message_ids
    ]
    if provider != "anthropic":
        return list(messages), PromptCacheMarkerResult(
            provider_id=provider,
            applied=False,
            marker_count=0,
            eligible_system_count=len(eligible_indices),
            stable_fingerprint=stable_fingerprint,
            reason="provider_not_anthropic",
        )
    if not eligible_indices:
        return list(messages), PromptCacheMarkerResult(
            provider_id=provider,
            applied=False,
            marker_count=0,
            eligible_system_count=0,
            stable_fingerprint=stable_fingerprint,
            reason="no_cache_eligible_system",
        )

    target_index = eligible_indices[-1]
    marked_messages = list(messages)
    marked_messages[target_index] = _with_anthropic_cache_marker(marked_messages[target_index])
    return marked_messages, PromptCacheMarkerResult(
        provider_id=provider,
        applied=True,
        marker_count=1,
        eligible_system_count=len(eligible_indices),
        stable_fingerprint=stable_fingerprint,
        reason="stable_system_breakpoint",
    )
