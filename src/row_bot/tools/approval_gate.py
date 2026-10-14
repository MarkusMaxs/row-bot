"""One place where an action that needs approval gets it, by the approval level.

- **Ask**: the person approves it on the approval card (a LangGraph interrupt).
- **Auto**: the reviewer (``approval_review``) decides first; it approves what
  clearly matches the person's request and otherwise asks the person, with its
  reason on the card.
- **Hands-off**: approved without asking, and recorded.

Every decision is recorded on the action's result as its approval line (B235),
so the conversation's activity shows "Approved by the reviewer: …" or "Allowed
by Hands-off". Tools that cannot lead their own result with it get it from
the agent's tool wrapper (``collect_decisions`` / ``with_decisions``).
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
from collections.abc import Iterable, Iterator
from typing import Any, Callable, Literal

from row_bot.approval_policy import decision_for_action, normalize_approval_mode

logger = logging.getLogger(__name__)

GateOutcome = Literal["allow", "block", "deny", "take_over"]

# The first line of every approval-gated result, so the model can say whether
# approval was needed, given or refused instead of guessing (B235).
APPROVAL_NOT_NEEDED_SAFE = "Approval: not needed (safe command)"
APPROVAL_NOT_NEEDED_READ_ONLY = "Approval: not needed (read-only)"
APPROVAL_NOT_NEEDED_BY_PERSON = "Approval: not needed (you started it)"
APPROVAL_GIVEN = "Approval: asked; approved by you"
APPROVAL_DENIED = "Approval: asked; denied by you — did not run"
APPROVAL_FULL_AUTONOMY = "Approval: allowed by Hands-off"
_APPROVAL_PREFIX = "Approval: "


def approved_by_reviewer(reason: str) -> str:
    """The approval line of an action the reviewer approved under Auto."""

    return f"Approval: approved by the reviewer: {_reason(reason)}"


def after_review(line: str, reason: str) -> str:
    """The person's answer after the reviewer asked them, with the reviewer's reason."""

    return f"{line}. The reviewer asked you: {_reason(reason)}"


def _reason(reason: str) -> str:
    text = " ".join(str(reason or "").split()).rstrip(".")
    return text or "no reason given"


# ── Recording decisions on the result ────────────────────────────────────────

_decisions_var: contextvars.ContextVar[list[str] | None] = contextvars.ContextVar(
    "approval_decisions", default=None
)


@contextlib.contextmanager
def collect_decisions() -> Iterator[list[str]]:
    """Collect the approval lines decided while one tool call runs."""

    decisions: list[str] = []
    token = _decisions_var.set(decisions)
    try:
        yield decisions
    finally:
        _decisions_var.reset(token)


def _record(line: str) -> None:
    decisions = _decisions_var.get()
    if decisions is not None and line:
        decisions.append(line)


def with_decisions(decisions: Iterable[str], result: Any) -> Any:
    """Lead a plain-text result with the approval lines decided for it, unless it has its own."""

    lines = [line[len(_APPROVAL_PREFIX):] if line.startswith(_APPROVAL_PREFIX) else line
             for line in decisions if line]
    if not lines or (isinstance(result, str) and result.startswith(_APPROVAL_PREFIX)):
        return result
    return with_approval(_APPROVAL_PREFIX + "; ".join(dict.fromkeys(lines)), result)


def with_approval(line: str, result: Any) -> Any:
    """Lead a plain-text result with its approval line.

    Structured results (JSON, media markers) are left as they are: the
    transcript reads them as data.
    """

    if not isinstance(result, str) or result.lstrip().startswith(("{", "[", "__")):
        return result
    return f"{line}\n{result}"


def current_approval_mode() -> str:
    """Return the active thread approval level, defaulting to Ask."""

    try:
        from row_bot.agent import get_approval_mode

        return get_approval_mode()
    except Exception:
        from row_bot.approval_policy import DEFAULT_APPROVAL_MODE

        return DEFAULT_APPROVAL_MODE


# ── Resumed runs ─────────────────────────────────────────────────────────────


def _answer_waiting() -> bool:
    """Whether this tool call is a re-run of one the person already answered.

    A LangGraph tool that asked runs again from the start once the person
    answers, and its ``interrupt()`` returns their answer. The reviewer must not
    decide again on that re-run (it could approve what the person denied), so a
    resumed task goes straight to the person's answer. Unknown is treated as
    resumed: the person is asked, never skipped.
    """

    try:
        from langgraph._internal._constants import CONFIG_KEY_SCRATCHPAD
        from langgraph.config import get_config
    except Exception:
        return True
    try:
        configurable = get_config().get("configurable") or {}
    except RuntimeError:
        return False  # Not inside a run: nothing can be waiting.
    scratchpad = configurable.get(CONFIG_KEY_SCRATCHPAD)
    if scratchpad is None:
        return False  # Not a graph task (a tool called on its own): nothing to resume.
    try:
        return bool(scratchpad.resume) or scratchpad.get_null_resume(False) is not None
    except Exception:
        logger.debug("Could not tell whether an answer is waiting; asking the person", exc_info=True)
        return True


# ── The decision ─────────────────────────────────────────────────────────────

# Why the reviewer asked, by action, so the person's answer on the re-run can say so.
_ASKED_REASONS: dict[str, str] = {}
_MAX_ASKED_REASONS = 256


def _asked_key(payload: dict[str, Any]) -> str:
    import hashlib

    try:
        from row_bot.agent import get_current_thread_id

        thread_id = get_current_thread_id()
    except Exception:
        thread_id = ""
    raw = "\0".join((thread_id, str(payload.get("tool") or ""), str(payload.get("label") or ""),
                      str(payload.get("description") or "")))
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()


def _remember_asked(key: str, reason: str) -> None:
    _ASKED_REASONS[key] = reason
    while len(_ASKED_REASONS) > _MAX_ASKED_REASONS:
        _ASKED_REASONS.pop(next(iter(_ASKED_REASONS)))


def _interrupt(payload: dict[str, Any]) -> Any:
    from langgraph.types import interrupt

    return interrupt(payload)


def _decide(
    payload: dict[str, Any],
    *,
    approval_mode: object,
    risks: Iterable[str] = (),
    approval_callback: Callable[[dict[str, Any]], Any] | None = None,
) -> tuple[Any, str]:
    """(the answer, its approval line) for an action that needs approval under the level.

    The answer is True when the level gave it; otherwise the person's own
    (True, False or "take over"). The line is empty when the person answered
    under Ask (callers keep their own lines for that).
    """

    mode = normalize_approval_mode(approval_mode)
    if mode == "full":
        return True, APPROVAL_FULL_AUTONOMY
    ask = approval_callback or _interrupt
    reason = ""
    if mode == "auto":
        key = _asked_key(payload)
        if _answer_waiting():
            # The person already answered this: the reviewer asked them first. Never review again.
            reason = _ASKED_REASONS.get(key, "")
        else:
            from row_bot.approval_review import review

            verdict = review(payload, risks=risks)
            if verdict.approved:
                return True, approved_by_reviewer(verdict.reason)
            reason = verdict.reason
            _remember_asked(key, reason)
            payload = {**payload, "review": {"decision": "ask", "reason": reason}}
    response = ask(payload)
    if mode != "auto":
        return response, ""
    line = APPROVAL_GIVEN if response is True else APPROVAL_DENIED
    return response, after_review(line, reason) if reason else line


def request_approval(
    payload: dict[str, Any],
    *,
    approval_mode: object = None,
    risks: Iterable[str] = (),
    approval_callback: Callable[[dict[str, Any]], Any] | None = None,
) -> Any:
    """Get the approval an action needs, by the level; the answer, as ``interrupt()`` returns it.

    For callers that already know the action needs approval (a Developer push,
    a settings change). True when Hands-off or the reviewer gave it, else
    the person's answer. The decision is recorded on the tool's result.
    ``risks``: the risk flags the caller already worked out, in plain words,
    for the reviewer.
    """

    mode = current_approval_mode() if approval_mode is None else approval_mode
    response, line = _decide(payload, approval_mode=mode, risks=risks, approval_callback=approval_callback)
    _record(line)
    return response


def _outcome(response: Any) -> GateOutcome:
    if response is True:
        return "allow"
    if str(response or "").strip().casefold() == "take over":
        return "take_over"
    return "deny"


def resolve_approval_line(
    payload: dict[str, Any],
    *,
    approval_mode: object,
    read_only: bool = False,
    risks: Iterable[str] = (),
    approval_callback: Callable[[dict[str, Any]], Any] | None = None,
) -> tuple[GateOutcome, str]:
    """Resolve one action through the shared levels: its outcome and approval line."""

    decision = decision_for_action(normalize_approval_mode(approval_mode), read_only=read_only)
    if decision == "allow":
        return "allow", APPROVAL_NOT_NEEDED_READ_ONLY
    if decision == "block":
        return "block", ""
    response, line = _decide(payload, approval_mode=approval_mode, risks=risks,
                             approval_callback=approval_callback)
    outcome = _outcome(response)
    if not line:
        line = APPROVAL_GIVEN if outcome == "allow" else APPROVAL_DENIED
    _record(line)
    return outcome, line


def resolve_approval(
    payload: dict[str, Any],
    *,
    approval_mode: object,
    read_only: bool = False,
    risks: Iterable[str] = (),
    approval_callback: Callable[[dict[str, Any]], bool | str] | None = None,
) -> GateOutcome:
    """Resolve one action through the shared Block/Ask/Auto/Hands-off levels.

    The optional callback keeps deterministic subsystem tests independent from
    LangGraph. Shipped callers omit it and use the normal graph interrupt.
    """

    outcome, _line = resolve_approval_line(
        payload,
        approval_mode=approval_mode,
        read_only=read_only,
        risks=risks,
        approval_callback=approval_callback,
    )
    return outcome


def approval_check(
    payload: dict[str, Any],
    *,
    read_only: bool = False,
    risks: Iterable[str] = (),
    blocked_message: str | None = None,
    cancelled_message: str = "Action cancelled by user.",
) -> tuple[str | None, str]:
    """Resolve one action: its refusal (None when it may run) and approval line.

    A tool that runs the action leads its result with the line; a refusal
    after a denial already carries it.
    """

    mode = current_approval_mode()
    decision, line = resolve_approval_line(
        payload,
        approval_mode=mode,
        read_only=read_only,
        risks=risks,
    )
    if decision == "allow":
        return None, line
    label = str(payload.get("label") or payload.get("tool") or "Action")
    if decision == "block":
        return blocked_message or (
            f"BLOCKED: {label} is unavailable while this thread is in Block approval mode."
        ), ""
    return with_approval(line, cancelled_message), line


def gate_action(
    payload: dict[str, Any],
    *,
    read_only: bool = False,
    risks: Iterable[str] = (),
    blocked_message: str | None = None,
    cancelled_message: str = "Action cancelled by user.",
) -> str | None:
    """Return None when the action may run, otherwise a user-facing refusal."""

    refusal, _line = approval_check(
        payload,
        read_only=read_only,
        risks=risks,
        blocked_message=blocked_message,
        cancelled_message=cancelled_message,
    )
    return refusal
