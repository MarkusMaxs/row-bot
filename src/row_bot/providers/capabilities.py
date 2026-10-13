from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from row_bot.providers.models import ModelInfo, ModelModality, ModelTask, TransportMode

CHAT_TASKS = {ModelTask.CHAT.value, ModelTask.RESPONSES.value}

SURFACE_REQUIREMENTS: dict[str, dict[str, set[str]]] = {
    "chat": {"tasks_any": CHAT_TASKS, "output_any": {ModelModality.TEXT.value}},
    "workflow": {"tasks_any": CHAT_TASKS, "output_any": {ModelModality.TEXT.value}},
    "channels": {"tasks_any": CHAT_TASKS, "output_any": {ModelModality.TEXT.value}},
    "designer": {"tasks_any": CHAT_TASKS, "output_any": {ModelModality.TEXT.value}},
    "status_tool": {"tasks_any": CHAT_TASKS, "output_any": {ModelModality.TEXT.value}},
    "vision": {"input_any": {ModelModality.IMAGE.value}, "output_any": {ModelModality.TEXT.value}},
    "image": {"tasks_any": {ModelTask.IMAGE_GENERATION.value, ModelTask.IMAGE_EDIT.value}, "output_any": {ModelModality.IMAGE.value}},
    "video": {"tasks_any": {ModelTask.VIDEO_GENERATION.value}, "output_any": {ModelModality.VIDEO.value}},
    "embeddings": {"tasks_any": {ModelTask.EMBEDDING.value}},
    "audio": {"tasks_any": {ModelTask.TRANSCRIPTION.value, ModelTask.TTS.value, ModelTask.REALTIME.value}},
    "voice": {"tasks_any": {ModelTask.TRANSCRIPTION.value, ModelTask.TTS.value, ModelTask.REALTIME.value}},
}


def normalize_str_set(value: Any) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, str):
        return {value} if value else set()
    if isinstance(value, (list, tuple, set, frozenset)):
        return {str(item) for item in value if str(item)}
    return set()


def normalize_snapshot(snapshot: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(snapshot, Mapping):
        return {}
    normalized = {
        "capabilities": normalize_str_set(snapshot.get("capabilities")),
        "input_modalities": normalize_str_set(snapshot.get("input_modalities")),
        "output_modalities": normalize_str_set(snapshot.get("output_modalities")),
        "tasks": normalize_str_set(snapshot.get("tasks")),
        "endpoint_compatibility": normalize_str_set(snapshot.get("endpoint_compatibility")),
        "tool_calling": snapshot.get("tool_calling"),
        "streaming": snapshot.get("streaming"),
        "transport": str(snapshot.get("transport") or ""),
        "source_confidence": str(snapshot.get("source_confidence") or ""),
    }
    if isinstance(snapshot.get("reasoning"), Mapping):
        normalized["reasoning"] = dict(snapshot["reasoning"])
    if isinstance(snapshot.get("generation_parameters"), Mapping):
        normalized["generation_parameters"] = dict(snapshot["generation_parameters"])
    return normalized


def snapshot_for_model(model_info: ModelInfo) -> dict[str, Any]:
    return model_info.capability_snapshot()


def snapshot_supports_surface(snapshot: Mapping[str, Any] | None, surface: str) -> bool:
    requirements = SURFACE_REQUIREMENTS.get(surface)
    normalized = normalize_snapshot(snapshot)
    if not requirements or not normalized:
        return True

    if not (
        normalized["tasks"]
        or normalized["input_modalities"]
        or normalized["output_modalities"]
        or normalized["capabilities"]
    ):
        return True

    tasks = normalized["tasks"]
    input_modalities = normalized["input_modalities"]
    output_modalities = normalized["output_modalities"]

    if "tasks_any" in requirements and not tasks.intersection(requirements["tasks_any"]):
        return False
    if "input_any" in requirements and not input_modalities.intersection(requirements["input_any"]):
        return False
    if "output_any" in requirements and not output_modalities.intersection(requirements["output_any"]):
        return False
    return True


def model_supports_surface(model_info: ModelInfo, surface: str) -> bool:
    return snapshot_supports_surface(snapshot_for_model(model_info), surface)


def visible_surfaces_for_model(model_info: ModelInfo) -> list[str]:
    surfaces = [surface for surface in SURFACE_REQUIREMENTS if model_supports_surface(model_info, surface)]
    if "chat" in surfaces:
        for default_surface in ("workflow", "channels", "designer", "status_tool"):
            if default_surface not in surfaces:
                surfaces.append(default_surface)
    return surfaces


def endpoint_values(modes: set[TransportMode] | frozenset[TransportMode] | tuple[TransportMode, ...]) -> frozenset[TransportMode]:
    return frozenset(modes)


# ── Prompt caching on Claude routes ─────────────────────────────────────────
# How each Claude route takes prompt-cache instructions (WP A3). Only routes whose
# documented request forms were checked are on; the rest send no cache
# instructions until an opt-in live check (WP B3) shows they accept them.
#
# - "anthropic": the direct Claude API. Block-level ``cache_control`` breakpoints
#   (with ``ttl``) and top-level automatic caching.
# - "openrouter": OpenRouter ``anthropic/claude-*`` models. OpenRouter documents
#   the same two forms (block-level ``cache_control`` on system and message
#   content parts, and a top-level ``cache_control``), both with ``ttl``.
# - "requesty": Requesty Claude routes. Requesty documents only its own flag,
#   ``"requesty": {"auto_cache": true}``, which places the breakpoints itself
#   (no ``ttl``).
#
# Off: MiniMax, OpenCode's Anthropic route, Atlas Cloud and Claude Subscription
# (a separate plan, D7).
PROMPT_CACHE_ROUTE_ANTHROPIC = "anthropic"
PROMPT_CACHE_ROUTE_OPENROUTER = "openrouter"
PROMPT_CACHE_ROUTE_REQUESTY = "requesty"
# Routes that take an explicit breakpoint on the system prompt.
PROMPT_CACHE_SYSTEM_BREAKPOINT_ROUTES = frozenset({PROMPT_CACHE_ROUTE_ANTHROPIC, PROMPT_CACHE_ROUTE_OPENROUTER})
_REQUESTY_CLAUDE_PREFIXES = ("anthropic/claude-", "bedrock/claude-", "vertex/claude-")


def prompt_cache_route(provider_id: str | None, model_id: str | None) -> str:
    """The prompt-cache route for a Claude model, or ``""`` when Row-Bot sends no cache instructions."""
    provider = str(provider_id or "").strip().lower()
    model = str(model_id or "").strip().lower()
    if provider == "anthropic":
        return PROMPT_CACHE_ROUTE_ANTHROPIC
    if provider == "openrouter" and model.startswith("anthropic/claude-"):
        return PROMPT_CACHE_ROUTE_OPENROUTER
    if provider == "requesty" and model.startswith(_REQUESTY_CLAUDE_PREFIXES):
        return PROMPT_CACHE_ROUTE_REQUESTY
    return ""
