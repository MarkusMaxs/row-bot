"""Preserved-thinking compliance for the direct Anthropic API.

On Claude Fable 5.1, Opus 5.5, Sonnet 5.5 and Haiku 5.5, a ``thinking`` or
``redacted_thinking`` block's signature is bound to the exact conversation
prefix that produced it: the top-level ``system``, the ``tools`` set and every
earlier message. Replaying a block after that prefix changed is a 400
``invalid_request_error`` for accounts created on or after 2026-08-31. Row-Bot
replays thinking blocks and still rewrites parts of its prefix between calls,
so the direct Anthropic route (``CancellableChatAnthropic`` with
``thinking_binding=True``) does three things:

1. **Drop instead of fail.** When a request runs adaptive thinking, it sends
   ``thinking.block_binding.prefix_mismatch_behavior = "drop_block"`` under the
   ``thinking-binding-controls-2026-08-01`` beta. The API then drops the first
   mismatched block and every later one, and reports each drop in the
   response's ``input_transformations``.
2. **Log the drops.** Each reported drop is logged at INFO with its path and
   reason, never content.
3. **Strip and retry once.** A 400 that names an invalid or
   conversation-bound thinking signature, or that rejects the controls
   themselves, is retried exactly once without thinking blocks (and without
   the controls when they were rejected). Other 400s are never retried.

"Runs adaptive thinking" means the payload already asks for
``{"type": "adaptive"}``, or it omits ``thinking`` on a model family where
omitting it runs adaptive thinking (``ADAPTIVE_BY_DEFAULT_MODEL_PREFIXES``).
Thinking is never turned on where the request didn't already have it:
``disabled``, ``enabled`` (budget), Sonnet 5.5's ``between_tools`` and older
models such as Opus 4.8, where omitting ``thinking`` means no thinking, are
left alone.

This module only shapes payloads and classifies errors. It has no network
access and imports nothing heavy, so ``reasoning_fallback`` can use it too.
"""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Mapping, Sequence
from typing import Any

logger = logging.getLogger(__name__)

THINKING_BINDING_BETA = "thinking-binding-controls-2026-08-01"
DROP_BLOCK_BINDING: dict[str, str] = {"prefix_mismatch_behavior": "drop_block"}

# Model families where omitting ``thinking`` runs adaptive thinking (Claude API
# reference, cached 2026-10-06): Fable 5 / 5.1, Mythos 5 / 5.1, Opus 5 / 5.5,
# Sonnet 5 / 5.5 and Haiku 5.5. Opus 4.8 / 4.7 and older are deliberately absent:
# omitting ``thinking`` there means no thinking. Mythos 5.1 doesn't run the
# conversation check, but it accepts the field and reports model-check drops.
ADAPTIVE_BY_DEFAULT_MODEL_PREFIXES: tuple[str, ...] = (
    "claude-fable-5",
    "claude-mythos-5",
    "claude-opus-5",
    "claude-sonnet-5",
    "claude-haiku-5",
)

_THINKING_BLOCK_TYPES = frozenset({"thinking", "redacted_thinking"})
_SIGNATURE_ERROR = re.compile(r"invalid\s+signature\s+in\s+(?:redacted_)?thinking\s+block")
_BOUND_ELSEWHERE = "bound to a different conversation"
_CONTROLS_REJECTED_TERMS = ("thinking-binding-controls", "block_binding")
_MAX_LOGGED_DROPS = 20

# Routes (API base URL, model) whose endpoint rejected the controls. Later
# requests on them skip the controls, so a rejecting endpoint costs one failed
# request per process instead of one per call.
_REJECTED_ROUTES: set[tuple[str, str]] = set()
_REJECTED_LOCK = threading.Lock()


def runs_adaptive_by_default(model: str) -> bool:
    """Return True when omitting ``thinking`` runs adaptive thinking on ``model``."""
    return _model_id(model).startswith(ADAPTIVE_BY_DEFAULT_MODEL_PREFIXES)


def with_thinking_binding(payload: dict[str, Any]) -> dict[str, Any]:
    """Return ``payload`` with ``drop_block`` binding controls when it runs adaptive thinking.

    Existing thinking fields (``display`` and any caller-chosen ``block_binding``)
    and existing betas are kept. Effort lives in ``output_config`` and is untouched.
    """
    model = _model_id(payload.get("model"))
    if not model.startswith("claude-"):
        return payload
    thinking = payload.get("thinking")
    if thinking is None:
        if not runs_adaptive_by_default(model):
            return payload
        bound: dict[str, Any] = {"type": "adaptive"}
    elif isinstance(thinking, Mapping) and thinking.get("type") == "adaptive":
        bound = dict(thinking)
    else:
        return payload
    bound.setdefault("block_binding", dict(DROP_BLOCK_BINDING))
    betas = [str(beta) for beta in payload.get("betas") or []]
    if THINKING_BINDING_BETA not in betas:
        betas.append(THINKING_BINDING_BETA)
    return {**payload, "thinking": bound, "betas": betas}


def without_thinking_binding(payload: dict[str, Any]) -> dict[str, Any]:
    """Return ``payload`` without the binding beta and ``block_binding``.

    An injected ``{"type": "adaptive"}`` stays: on the models that get one it is
    equivalent to omitting ``thinking``.
    """
    result = dict(payload)
    betas = [beta for beta in result.get("betas") or [] if beta != THINKING_BINDING_BETA]
    if betas:
        result["betas"] = betas
    else:
        result.pop("betas", None)
    thinking = result.get("thinking")
    if isinstance(thinking, Mapping) and "block_binding" in thinking:
        result["thinking"] = {key: value for key, value in thinking.items() if key != "block_binding"}
    return result


def has_thinking_binding(payload: Mapping[str, Any]) -> bool:
    thinking = payload.get("thinking")
    return THINKING_BINDING_BETA in (payload.get("betas") or []) or (
        isinstance(thinking, Mapping) and "block_binding" in thinking
    )


def strip_thinking_blocks(messages: Sequence[Any] | None) -> tuple[list[Any], int]:
    """Remove every ``thinking`` and ``redacted_thinking`` block from Anthropic messages.

    Each turn's text and tool_use blocks stay. An assistant turn that held only
    reasoning has nothing left to replay and is left out; the API joins the
    neighbouring user turns. Returns the new messages and the number of blocks
    removed. The input is not modified.
    """
    stripped = 0
    result: list[Any] = []
    for message in messages or []:
        content = message.get("content") if isinstance(message, Mapping) else None
        if not isinstance(content, list):
            result.append(message)
            continue
        kept = [
            block
            for block in content
            if not (isinstance(block, Mapping) and block.get("type") in _THINKING_BLOCK_TYPES)
        ]
        removed = len(content) - len(kept)
        if not removed:
            result.append(message)
            continue
        stripped += removed
        if kept:
            result.append({**message, "content": kept})
    return result, stripped


def is_thinking_signature_error(exc: BaseException) -> bool:
    """A 400 that rejects a replayed thinking block's signature.

    Covers the conversation-binding failure ("... The block is bound to a
    different conversation.") and a tampered or undecryptable signature (the
    same leading clause without that sentence). Removing the blocks clears both.
    """
    if _status_code(exc) != 400:
        return False
    text = _error_text(exc)
    return bool(_SIGNATURE_ERROR.search(text)) or _BOUND_ELSEWHERE in text


def is_thinking_binding_rejection(exc: BaseException) -> bool:
    """A 400 that rejects the binding beta name or the ``block_binding`` field."""
    if _status_code(exc) != 400 or is_thinking_signature_error(exc):
        return False
    text = _error_text(exc)
    return any(term in text for term in _CONTROLS_REJECTED_TERMS)


def is_thinking_binding_error(exc: BaseException) -> bool:
    """Either 400 above. ``reasoning_fallback`` never retries these with Provider default."""
    return is_thinking_signature_error(exc) or is_thinking_binding_rejection(exc)


def thinking_recovery_payload(
    payload: Mapping[str, Any],
    exc: BaseException,
) -> tuple[dict[str, Any], int, bool] | None:
    """The one retry for a thinking-binding 400, or None when there is none to make.

    Returns ``(retry_payload, stripped_blocks, controls_rejected)``. The retry
    always drops the thinking blocks; it also drops the controls when the
    endpoint rejected them. Unrelated 400s, and a retry identical to the failed
    request, return None.
    """
    signature = is_thinking_signature_error(exc)
    rejected = not signature and has_thinking_binding(payload) and is_thinking_binding_rejection(exc)
    if not (signature or rejected):
        return None
    messages, stripped = strip_thinking_blocks(payload.get("messages"))
    if not stripped and not rejected:
        return None
    retry: dict[str, Any] = {**payload, "messages": messages}
    if rejected:
        retry = without_thinking_binding(retry)
    return retry, stripped, rejected


def thinking_binding_rejected(route: tuple[str, str]) -> bool:
    with _REJECTED_LOCK:
        return route in _REJECTED_ROUTES


def remember_thinking_binding_rejected(route: tuple[str, str]) -> None:
    with _REJECTED_LOCK:
        _REJECTED_ROUTES.add(route)


def log_input_transformations(transformations: Any, *, model: str) -> int:
    """Log the thinking blocks the API dropped, by path and reason only.

    Returns the number of ``thinking_dropped`` entries. Entry types and reasons
    this code doesn't know are logged at DEBUG and otherwise ignored, because
    the API adds new ones over time.
    """
    if not isinstance(transformations, (list, tuple)):
        return 0
    drops: list[str] = []
    for entry in transformations:
        fields = _as_mapping(entry)
        kind = _short(fields.get("type"))
        path = _short(fields.get("path"))
        reason = _short(fields.get("reason"))
        if kind == "thinking_dropped":
            drops.append(f"{path}:{reason}")
        else:
            logger.debug(
                "anthropic_thinking_binding: input transformation type=%s path=%s reason=%s model=%s",
                kind,
                path,
                reason,
                model,
            )
    if drops:
        logger.info(
            "anthropic_thinking_binding: API dropped %d thinking block(s) model=%s drops=%s%s",
            len(drops),
            model,
            ", ".join(drops[:_MAX_LOGGED_DROPS]),
            "" if len(drops) <= _MAX_LOGGED_DROPS else f" (+{len(drops) - _MAX_LOGGED_DROPS} more)",
        )
    return len(drops)


def _model_id(model: Any) -> str:
    return str(model or "").strip().lower()


def _as_mapping(entry: Any) -> Mapping[str, Any]:
    if isinstance(entry, Mapping):
        return entry
    dump = getattr(entry, "model_dump", None)
    if callable(dump):
        try:
            dumped = dump()
        except Exception:
            return {}
        return dumped if isinstance(dumped, Mapping) else {}
    return {}


def _short(value: Any) -> str:
    # Paths and reasons are short API identifiers; bound them anyway so a log
    # line can never carry anything larger.
    return str(value or "")[:80]


def _status_code(exc: BaseException) -> int:
    for candidate in (
        getattr(exc, "status_code", None),
        getattr(getattr(exc, "response", None), "status_code", None),
    ):
        try:
            status = int(candidate or 0)
        except (TypeError, ValueError):
            status = 0
        if status:
            return status
    return 0


def _error_text(exc: BaseException) -> str:
    parts = [str(exc), str(getattr(exc, "message", "") or "")]
    body = getattr(exc, "body", None)
    if isinstance(body, Mapping):
        error = body.get("error")
        if isinstance(error, Mapping):
            parts.append(str(error.get("message") or ""))
        parts.append(str(body.get("message") or ""))
    return " ".join(parts).replace("`", "").lower()
