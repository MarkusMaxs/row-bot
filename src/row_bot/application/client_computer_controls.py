"""Computer use in the conversation: what Row-Bot is doing, the latest picture, and Stop.

One computer use session exists at a time, and only the conversation whose
turn holds it sees it. The picture stays in memory: it is read from the
session on request and is never written to disk, logged or kept in a command
receipt. Stop releases the computer and ends the turn. When computer use
couldn't start in this conversation (not set up, no permission), the card
says what's wrong and how to fix it.
"""

from __future__ import annotations

import base64
from collections.abc import Callable
from copy import deepcopy
import hashlib
import json
import logging
import re
import sys
from typing import Any
import uuid

from row_bot.application.client_platform import ClientPlatformError
from row_bot.runtime import admissions


ACTIONS = frozenset({"computer_use.stop"})
_REVISION = re.compile(r"[0-9a-f]{64}")
_MAX_PICTURE_BYTES = 1_500_000
_PICTURE_TYPES = frozenset({"image/png", "image/jpeg"})
_LOG = logging.getLogger(__name__)


class ClientComputerControlError(ClientPlatformError):
    """Stable, client-safe computer-use error."""


def _error(code: str, revision: str | None = None) -> ClientComputerControlError:
    return ClientComputerControlError(code, current_revision=revision)


def _service(computer: Any | None) -> Any | None:
    """The computer-use service, only when something could have started it.

    A passive read must not construct the service: when its module was never
    loaded, no session can exist.
    """

    if computer is not None:
        return computer
    module = sys.modules.get("row_bot.computer_use.service")
    return module.get_computer_use_service() if module is not None else None


def _status(computer: Any | None) -> dict[str, Any]:
    raw = computer.status_snapshot() if computer is not None else {}
    return raw if isinstance(raw, dict) else {}


def _belongs(raw: dict[str, Any], conversation_id: str) -> bool:
    return raw.get("active") is True and raw.get("thread_id") == conversation_id


def _plain(value: object, maximum: int) -> str:
    text = str(value or "").strip()
    return "".join(character for character in text if ord(character) >= 32)[:maximum]


def _identifier(value: object) -> str:
    if type(value) is not str or not value or len(value) > 128:
        raise _error("invalid_computer_use_command")
    return value


def _uuid(value: object) -> str:
    try:
        if type(value) is not str or str(uuid.UUID(value)) != value:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise _error("invalid_computer_use_command") from None
    return value


def stop_computer_use(conversation_id: str, *, generation_id: str = "", computer: Any | None = None) -> list[str]:
    """Release the conversation's computer (with ``generation_id``, only that turn's).

    Returns the approvals withdrawn here: none (the turn's own are withdrawn with it).
    """

    service = _service(computer)
    if service is not None:
        if not generation_id:
            service.close_for_thread(conversation_id)
        else:
            raw = _status(service)
            if _belongs(raw, conversation_id) and raw.get("generation_id") == generation_id:
                service.stop()
    return []


def release_after_turn(
    conversation_id: str, generation_id: str, status: str, *, computer: Any | None = None
) -> None:
    """A finished turn gives the computer back; a turn waiting on the person keeps it."""

    if status == "waiting_approval":
        return
    service = _service(computer)
    raw = _status(service)
    if _belongs(raw, conversation_id) and raw.get("generation_id") == generation_id:
        service.stop()


def prepare_approval_resume(
    platform: Any,
    conversation_id: str,
    context: dict[str, Any],
    *,
    approved: bool,
    runtime_surface: str = "normal_chat",
    computer: Any | None = None,
) -> str:
    """The turn whose computer session the continuing turn keeps after an answer, or "".

    The session waits while the person answers (an approval, "Carry on?"), and
    the turn that goes on keeps it.
    """

    del platform, approved, runtime_surface
    raw = _status(_service(computer))
    generation_id = str(context.get("generation_id") or "")
    if generation_id and _belongs(raw, conversation_id) and raw.get("generation_id") == generation_id:
        return generation_id
    return ""


def approval_context(raw: object) -> dict[str, Any]:
    try:
        context = json.loads(str(raw or "{}"))
    except (TypeError, ValueError):
        return {}
    return context if isinstance(context, dict) else {}


_STATES = {"working": "working", "paused": "paused", "waiting_approval": "waiting_approval"}


def _picture(service: Any | None) -> tuple[bytes, str, int] | None:
    reader = getattr(service, "ephemeral_picture", None)
    picture = reader() if callable(reader) else None
    if (
        not isinstance(picture, tuple)
        or len(picture) != 3
        or not isinstance(picture[0], bytes)
        or not picture[0]
        or len(picture[0]) > _MAX_PICTURE_BYTES
        or picture[1] not in _PICTURE_TYPES
    ):
        return None
    return picture


def _current_turn(platform: Any, conversation_id: str) -> str | None:
    """The conversation's latest turn (None when there's no platform to ask)."""

    projection = getattr(platform, "projection", None)
    if projection is None:
        return None
    generation = projection.snapshot(conversation_id).get("generation") or {}
    return str(generation.get("generation_id") or "")


def _snapshot(
    platform: Any, conversation_id: str, service: Any | None, *, with_picture: bool = False
) -> tuple[dict[str, Any], tuple[bytes, str, int] | None]:
    raw = _status(service)
    belongs = _belongs(raw, conversation_id)
    state = _STATES.get(str(raw.get("state") or ""), "working") if belongs else "stopped"
    has_picture = belongs and state != "paused" and bool(raw.get("has_thumbnail"))
    picture = _picture(service) if with_picture and has_picture else None
    problem = None
    if not belongs and service is not None:
        found = getattr(service, "problem", lambda _thread: None)(conversation_id)
        turn = _current_turn(platform, conversation_id) if found is not None else None
        # Shown with the turn that hit it: a later turn in the chat starts clean.
        if found is not None and (turn is None or found[1] == turn):
            problem = {"message": _plain(found[0].message, 240), "fix": _plain(found[0].fix, 240)}
    public: dict[str, Any] = {
        "schema_version": 1,
        "conversation_id": conversation_id,
        "active": belongs,
        "state": state,
        "app": _plain(raw.get("app"), 120) if belongs else "",
        "window": _plain(raw.get("window"), 200) if belongs else "",
        "doing": _plain(raw.get("doing"), 200) if belongs and state == "working" else "",
        "has_picture": has_picture,
        "problem": problem,
        "can_stop": belongs,
    }
    try:
        activity = max(0, int(raw.get("revision") or 0))
    except (TypeError, ValueError, OverflowError):
        activity = 0
    proof = {**public, "activity_revision": activity,
             "generation_id": str(raw.get("generation_id") or "") if belongs else ""}
    public["revision"] = hashlib.sha256(
        json.dumps(proof, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return public, picture


def read_computer_controls(
    platform: Any,
    conversation_id: str,
    *,
    validate: Callable[[], None],
    computer: Any | None = None,
) -> dict[str, Any]:
    """Read the card's state without starting, capturing or changing anything."""

    validate()
    result = _snapshot(platform, _identifier(conversation_id), _service(computer))[0]
    validate()
    return result


def read_computer_preview(
    platform: Any,
    conversation_id: str,
    expected_revision: str,
    *,
    validate: Callable[[], None],
    computer: Any | None = None,
) -> dict[str, Any]:
    """The latest picture, and the step's revision now (the one the step read, or a later one).

    Reading it may take a new picture when the last one is a second or two old (the live view).
    """

    if type(expected_revision) is not str or not _REVISION.fullmatch(expected_revision):
        raise _error("invalid_computer_use_command")
    validate()
    conversation_id = _identifier(conversation_id)
    public, picture = _snapshot(platform, conversation_id, _service(computer), with_picture=True)
    state = (
        "inactive" if not public["active"]
        else "hidden" if public["state"] == "paused"
        else "available" if picture is not None
        else "waiting"
    )
    validate()
    return {
        "schema_version": 1,
        "conversation_id": conversation_id,
        "revision": public["revision"],
        "state": state,
        "mime_type": picture[1] if state == "available" else None,
        "image_base64": base64.b64encode(picture[0]).decode("ascii") if state == "available" else None,
    }


def _public_receipt(saved: object) -> dict[str, Any]:
    fields = {"schema_version", "command_id", "action", "conversation_id", "status", "code", "computer_use"}
    if not isinstance(saved, dict) or not fields <= set(saved):
        raise _error("computer_use_outcome_uncertain")
    return deepcopy({name: saved[name] for name in fields})


def execute_computer_command(
    platform: Any,
    command: dict[str, Any],
    conversation_id: str,
    *,
    owner_id: str,
    key: str,
    validate: Callable[[], None],
    computer: Any | None = None,
) -> dict[str, Any]:
    """Run one Stop; a repeated command reads its first outcome."""

    validate()
    conversation_id = _identifier(conversation_id)
    owner_id = _identifier(owner_id)
    command_id = _uuid(command.get("command_id"))
    action = command.get("type")
    if action not in ACTIONS or key != command_id:
        raise _error("invalid_computer_use_command")
    receipt: dict[str, Any] = {
        "schema_version": 1,
        "command_id": command_id,
        "action": action,
        "conversation_id": conversation_id,
        "status": "partial",
        "code": "computer_use_outcome_uncertain",
        "computer_use": None,
    }
    try:
        prior = admissions.claim_command(
            owner_id,
            key,
            {"command_id": command_id, "type": action},
            f"computer-use:{conversation_id}",
            initial_result=receipt,
        )
    except admissions.AdmissionError as exc:
        if str(exc) != "operation_uncertain":
            raise _error(str(exc), exc.current_revision) from None
        # Interrupted before it finished: report that, never run it again.
        return _public_receipt(admissions.read_command_receipt(owner_id, command_id))
    if prior is not None:
        return _public_receipt(prior)
    try:
        validate()
    except Exception as exc:
        admissions.reject_command(owner_id, key, str(getattr(exc, "code", "authentication_required")))
        raise
    try:
        # Stop is always safe: it releases the computer and ends the turn.
        platform.stop_conversation(conversation_id)
        receipt.update(status="completed", code=None)
    except ClientPlatformError as exc:
        receipt.update(status="rejected", code=exc.code)
    except Exception as exc:
        # Only the type: messages can carry app or window details.
        _LOG.warning("Computer-use %s did not finish cleanly (%s)", action, type(exc).__name__)
    try:
        receipt["computer_use"] = _snapshot(platform, conversation_id, _service(computer))[0]
    except Exception:
        _LOG.debug("Computer-use state after %s is unavailable", action, exc_info=True)
    admissions.complete_command(owner_id, key, receipt)
    return deepcopy(receipt)
