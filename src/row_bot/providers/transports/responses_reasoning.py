"""Encrypted reasoning replay for Row-Bot's own Responses transports (Codex, xAI OAuth).

With ``store: false`` nothing is kept server-side, so a reasoning model only
keeps its reasoning across the calls of a turn (and the cache keeps its
prefix) when the client sends the reasoning items back. The transports ask for
``reasoning.encrypted_content``, keep each reasoning item on the AIMessage they
return, and replay the items unchanged on later requests.

The items live in ``AIMessage.additional_kwargs[REASONING_REPLAY_KEY]``, tagged
with the transport and model that produced them. They are replayed only to that
same transport and model. No LangChain integration reads this key, so other
providers never receive the encrypted content.
"""

from __future__ import annotations

import copy
import logging
import re
from typing import Any, Iterable

logger = logging.getLogger(__name__)

REASONING_REPLAY_KEY = "responses_reasoning_replay"
ENCRYPTED_REASONING_INCLUDE = "reasoning.encrypted_content"
# The fields of a reasoning output item that are input again. ``status`` is
# output-only, and Codex CLI leaves it out of replayed items too.
_REPLAYED_FIELDS = ("type", "id", "summary", "content", "encrypted_content")


def replayable_reasoning_item(item: Any) -> dict[str, Any] | None:
    """The input form of a reasoning output item, or ``None`` when it can't be replayed."""
    if not isinstance(item, dict) or item.get("type") != "reasoning":
        return None
    encrypted = item.get("encrypted_content")
    if not isinstance(encrypted, str) or not encrypted:
        # Without server-side storage an item without its ciphertext can't be resolved.
        return None
    replayed = {key: copy.deepcopy(item[key]) for key in _REPLAYED_FIELDS if item.get(key) is not None}
    replayed.setdefault("summary", [])
    return replayed


def reasoning_items_from_events(events: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """The replayable reasoning items among a response's ``output_item.done`` events."""
    items: list[dict[str, Any]] = []
    for event in events:
        if event.get("type") != "response.output_item.done":
            continue
        replayed = replayable_reasoning_item(event.get("item"))
        if replayed is not None:
            items.append(replayed)
    return items


def reasoning_replay_kwargs(transport: str, model: str, items: list[dict[str, Any]]) -> dict[str, Any]:
    """``additional_kwargs`` that keep a response's reasoning items for replay."""
    if not items:
        return {}
    return {REASONING_REPLAY_KEY: {"transport": transport, "model": model, "items": items}}


def reasoning_items_for_replay(message: Any, *, transport: str, model: str) -> list[dict[str, Any]]:
    """The reasoning items an earlier AIMessage keeps for this transport and model.

    Items from another transport or model are never replayed: their ciphertext
    belongs to a different provider account or model.
    """
    additional_kwargs = getattr(message, "additional_kwargs", None) or {}
    stored = additional_kwargs.get(REASONING_REPLAY_KEY)
    if not isinstance(stored, dict):
        return []
    if stored.get("transport") != transport or stored.get("model") != model:
        return []
    items = stored.get("items")
    if not isinstance(items, list):
        return []
    return [replayed for item in items if (replayed := replayable_reasoning_item(item)) is not None]


def without_replayed_reasoning(body: dict[str, Any]) -> dict[str, Any] | None:
    """A copy of a request body without its replayed reasoning items, or ``None`` if it had none."""
    items = body.get("input")
    if not isinstance(items, list):
        return None
    kept = [item for item in items if not (isinstance(item, dict) and item.get("type") == "reasoning")]
    if len(kept) == len(items):
        return None
    return {**body, "input": kept}


_HTTP_400 = re.compile(r"\b(?:http|status(?: code)?:?)\s*400\b", re.IGNORECASE)


def is_encrypted_reasoning_rejection(exc: BaseException) -> bool:
    """True for a 400 that rejects replayed encrypted reasoning.

    That happens when the ciphertext can no longer be read, for example after
    the account behind the conversation changed. The request is then retried
    once without the replayed reasoning, which loses only earlier reasoning.
    """
    status = 0
    for candidate in (getattr(exc, "status_code", None), getattr(getattr(exc, "response", None), "status_code", None)):
        if isinstance(candidate, int) and candidate:
            status = candidate
            break
    text = str(exc)
    message = getattr(exc, "message", None)
    if isinstance(message, str):
        text = f"{text} {message}"
    if status != 400 and not (status == 0 and _HTTP_400.search(text)):
        return False
    return "encrypted" in text.lower()
