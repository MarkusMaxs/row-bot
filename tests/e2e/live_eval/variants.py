"""A/B configurations for a live eval run: test-only patches, never user settings.

``ROW_BOT_LIVE_EVAL_VARIANT`` (or ``scripts/live_eval.py --variant``) is a
comma-separated list of presets and patches, applied inside the eval worker
process before any task runs. The worker process ends with the run, so nothing
outlives it.

Presets:

- ``baseline``: Row-Bot as shipped.
- ``cache-key-thread`` / ``cache-key-none`` / ``cache-key-install`` (B2 decision 1):
  the cache-routing key (OpenAI ``prompt_cache_key``, xAI ``x-grok-conv-id``,
  OpenRouter ``session_id``, OpenCode session) per conversation (shipped), not
  sent at all, or one key shared by every conversation of the install.
- ``a4a-jump-share=<fraction>``, ``a4a-jump-min=<tokens>``,
  ``a4a-jump-max=<tokens>``, ``a4a-keep-turns=<n>``: A4a's clearing thresholds.
- ``headroom=<tokens>``: C1's reasoning headroom on capped background calls.
- ``thinking-error``: A2b's ``prefix_mismatch_behavior`` set to ``"error"``
  instead of ``"drop_block"``, so a history edit fails loudly (and is
  recovered by the one strip-and-retry).
- ``inflate-context=<factor>``: Row-Bot's compaction point and usable input
  multiplied by *factor*, so a small-window model overflows at the provider
  (A4b's reactive overflow check).

Patches, for anything else (A6 and later): ``patch:<module>:<ATTRIBUTE>=<json>``,
for example ``patch:row_bot.history_hygiene:KEEP_PERSON_TURNS=3``. The module
must be a ``row_bot`` module and the attribute must already exist.
"""
from __future__ import annotations

import dataclasses
import importlib
import json
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

VARIANT_ENV = "ROW_BOT_LIVE_EVAL_VARIANT"


class VariantError(ValueError):
    """A variant string names an unknown preset or a malformed patch."""


@dataclass(frozen=True)
class Patch:
    module: str
    attribute: str
    value: Any


@dataclass(frozen=True)
class Variant:
    """A parsed variant: its label, the attribute patches, and named hooks."""

    label: str = "baseline"
    patches: tuple[Patch, ...] = ()
    hooks: tuple[str, ...] = ()

    def describe(self) -> str:
        parts = [f"{p.module}.{p.attribute}={p.value!r}" for p in self.patches] + list(self.hooks)
        return f"{self.label}: " + (", ".join(parts) if parts else "Row-Bot as shipped")


_HISTORY = "row_bot.history_hygiene"
# Constants read at call time: a plain attribute patch reaches every caller.
_NUMERIC_PRESETS: dict[str, tuple[str, str, Callable[[str], Any]]] = {
    "a4a-jump-share": (_HISTORY, "JUMP_SHARE", float),
    "a4a-jump-min": (_HISTORY, "JUMP_MIN_TOKENS", int),
    "a4a-jump-max": (_HISTORY, "JUMP_MAX_TOKENS", int),
}
# Values bound when a function or dataclass was defined: applied by wrapping the function.
_WRAPPED_PRESETS: dict[str, Callable[[str], Any]] = {"a4a-keep-turns": int, "headroom": int, "inflate-context": float}
_HOOK_PRESETS = {"cache-key-thread", "cache-key-none", "cache-key-install", "thinking-error"}


def parse(text: str | None) -> Variant:
    """Parse a variant string; an empty string is the baseline."""
    raw = str(text or "").strip()
    if not raw or raw == "baseline":
        return Variant()
    patches: list[Patch] = []
    hooks: list[str] = []
    for item in (part.strip() for part in raw.split(",")):
        if not item or item == "baseline":
            continue
        if item in _HOOK_PRESETS:
            if item.startswith("cache-key-") and any(hook.startswith("cache-key-") for hook in hooks):
                raise VariantError("choose one cache-key preset")
            hooks.append(item)
            continue
        if item.startswith("patch:"):
            target, separator, value_text = item[len("patch:"):].partition("=")
            module, _, attribute = target.rpartition(":")
            if not separator or not module or not attribute:
                raise VariantError(f"{item!r}: write patch:<module>:<ATTRIBUTE>=<json value>")
            if not (module == "row_bot" or module.startswith("row_bot.")):
                raise VariantError(f"{item!r}: only row_bot modules can be patched")
            try:
                value = json.loads(value_text)
            except json.JSONDecodeError as exc:
                raise VariantError(f"{item!r}: the value must be JSON") from exc
            patches.append(Patch(module, attribute, value))
            continue
        name, separator, value_text = item.partition("=")
        kind = _NUMERIC_PRESETS[name][2] if name in _NUMERIC_PRESETS else _WRAPPED_PRESETS.get(name)
        if kind is not None and separator:
            try:
                value = kind(value_text)
            except ValueError as exc:
                raise VariantError(f"{item!r}: {value_text!r} is not a {kind.__name__}") from exc
            if value < 0:
                raise VariantError(f"{item!r}: must not be negative")
            if name in _NUMERIC_PRESETS:
                module, attribute, _ = _NUMERIC_PRESETS[name]
                patches.append(Patch(module, attribute, value))
            else:
                hooks.append(f"{name}={value}")
            continue
        presets = [*_NUMERIC_PRESETS, *_WRAPPED_PRESETS]
        known = sorted([*_HOOK_PRESETS, *(f"{name}=<value>" for name in presets), "patch:<module>:<ATTR>=<json>"])
        raise VariantError(f"unknown variant {item!r}; known: {', '.join(known)}")
    return Variant(label=raw, patches=tuple(patches), hooks=tuple(hooks))


def from_env(env: Mapping[str, str] | None = None) -> Variant:
    source = os.environ if env is None else env
    return parse(source.get(VARIANT_ENV, ""))


@dataclass
class Applied:
    """What :func:`apply` changed, so it can be undone (tests) and reported."""

    undo: list[Callable[[], None]] = field(default_factory=list)
    changes: list[str] = field(default_factory=list)

    def restore(self) -> None:
        while self.undo:
            self.undo.pop()()


def _set(module_name: str, attribute: str, value: Any, applied: Applied) -> None:
    module = importlib.import_module(module_name)
    if not hasattr(module, attribute):
        raise VariantError(f"{module_name} has no attribute {attribute}")
    original = getattr(module, attribute)
    setattr(module, attribute, value)
    applied.undo.append(lambda: setattr(module, attribute, original))
    applied.changes.append(f"{module_name}.{attribute}: {original!r} -> {value!r}")


def _replace_function_everywhere(module_name: str, name: str, replacement: Callable, applied: Applied) -> None:
    """Point every loaded row_bot module's reference to a function at *replacement*.

    Modules that imported the function by name hold their own reference, so
    patching only the defining module would miss them.
    """
    original = getattr(importlib.import_module(module_name), name)
    for loaded_name, loaded in list(sys.modules.items()):
        if loaded is None or not (loaded_name == "row_bot" or loaded_name.startswith("row_bot.")):
            continue
        namespace = getattr(loaded, "__dict__", {})
        for attribute, value in list(namespace.items()):
            if value is original:
                setattr(loaded, attribute, replacement)
                applied.undo.append(lambda loaded=loaded, attribute=attribute: setattr(loaded, attribute, original))
    applied.changes.append(f"{module_name}.{name} replaced in every loaded row_bot module")


def _wrap_background_headroom(headroom: int, applied: Applied) -> None:
    """C1's reasoning room is a dataclass default, so change it on each result instead."""
    background = importlib.import_module("row_bot.providers.background")
    original = background.background_reasoning
    default = background.REASONING_HEADROOM_TOKENS

    def with_headroom(*args: Any, **kwargs: Any) -> Any:
        result = original(*args, **kwargs)
        # Only the default room changes; a room derived from a thinking budget stays as computed.
        if getattr(result, "headroom", None) == default:
            return dataclasses.replace(result, headroom=headroom)
        return result

    _replace_function_everywhere("row_bot.providers.background", "background_reasoning", with_headroom, applied)


def _wrap_context_policy(factor: float, applied: Applied) -> None:
    """Make Row-Bot believe the window is *factor* times larger than it is (A4b reactive overflow)."""
    models = importlib.import_module("row_bot.models")
    original = models.get_context_policy

    def inflated(*args: Any, **kwargs: Any) -> Any:
        policy = original(*args, **kwargs)
        changes = {
            name: int(getattr(policy, name) * factor)
            for name in ("compact_at_tokens", "usable_input_tokens")
            if isinstance(getattr(policy, name, None), int)
        }
        return dataclasses.replace(policy, **changes) if changes else policy

    _replace_function_everywhere("row_bot.models", "get_context_policy", inflated, applied)


def apply(variant: Variant) -> Applied:
    """Apply *variant* to the loaded Row-Bot modules. Call after importing ``row_bot.agent``."""
    applied = Applied()
    try:
        for patch in variant.patches:
            _set(patch.module, patch.attribute, patch.value, applied)
        for hook in variant.hooks:
            if hook == "cache-key-none":
                _replace_function_everywhere(
                    "row_bot.providers.cache_affinity", "current_conversation_id", lambda: "", applied
                )
            elif hook == "cache-key-install":
                _replace_function_everywhere(
                    "row_bot.providers.cache_affinity", "current_conversation_id", lambda: "row-bot-install", applied
                )
            elif hook.startswith("a4a-keep-turns="):
                keep = int(hook.partition("=")[2])
                original_start = importlib.import_module(_HISTORY).protected_start
                _replace_function_everywhere(
                    _HISTORY, "protected_start",
                    lambda messages, keep_turns=keep, _original=original_start: _original(messages, keep_turns),
                    applied,
                )
            elif hook.startswith("headroom="):
                _wrap_background_headroom(int(hook.partition("=")[2]), applied)
            elif hook.startswith("inflate-context="):
                _wrap_context_policy(float(hook.partition("=")[2]), applied)
            elif hook == "thinking-error":
                _set(
                    "row_bot.providers.transports.thinking_binding",
                    "DROP_BLOCK_BINDING",
                    {"prefix_mismatch_behavior": "error"},
                    applied,
                )
            # cache-key-thread is the shipped behaviour: nothing to change.
    except Exception:
        applied.restore()
        raise
    return applied
