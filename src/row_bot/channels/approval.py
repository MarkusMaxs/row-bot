"""
Row-Bot – Shared Approval / Interrupt Helpers
=============================================
Centralised logic used by *all* channels to:

* **Resume** a paused agent graph after user approval/denial
* **Format** interrupt details as plain text
* **Extract** interrupt IDs for multi-interrupt resume
* **Classify** user text as approval / denial / unrelated

Any new channel adapter only needs to import these helpers rather than
re-implementing the resume logic.
"""

from __future__ import annotations

import logging
import re
from typing import Any

log = logging.getLogger("row_bot.channels.approval")


# ── Agent resume ─────────────────────────────────────────────────────

def resume_agent_sync(
    config: dict,
    approved: bool,
    *,
    interrupt_ids: list[str] | None = None,
) -> tuple[str, Any, list]:
    """Resume a paused agent graph after interrupt approval/denial.

    Mirrors the event-collection logic of each channel's
    ``_run_agent_sync`` but calls ``agent_mod.resume_stream_agent``
    instead of ``stream_agent``.

    Returns ``(answer_text, new_interrupt_data_or_None, captured_images)``.
    """
    import row_bot.agent as agent_mod
    from row_bot.channels.runtime import approval_mode_for_config
    from row_bot.tools import registry as tool_registry

    enabled = [t.name for t in tool_registry.get_enabled_tools()]
    config = {
        **config,
        "configurable": {
            **(config.get("configurable") or {}),
            "runtime_surface": "approval",
            "runtime_mode": "agent",
            "approval_mode": approval_mode_for_config(config),
        },
    }
    answer_tokens: list[str] = []
    interrupt_data: Any = None

    for event_type, payload in agent_mod.resume_stream_agent(
        enabled, config, approved, interrupt_ids=interrupt_ids
    ):
        if event_type == "token":
            answer_tokens.append(payload)
        elif event_type == "interrupt":
            interrupt_data = payload
        elif event_type == "done":
            if payload and not answer_tokens:
                answer_tokens.append(payload)

    answer = "".join(answer_tokens).strip()
    return answer, interrupt_data, []


# ── Interrupt formatting ─────────────────────────────────────────────

def format_interrupt_text(data: Any) -> str:
    """Format interrupt data as **plain text** suitable for any channel.

    Telegram has its own HTML formatter; use this for text-based channels
    (Slack, Discord, WhatsApp, SMS).
    """
    items = data if isinstance(data, list) else [data]
    parts: list[str] = []

    for item in items:
        if not isinstance(item, dict):
            parts.append(str(item))
            continue
        tool_name = item.get("tool", item.get("name", "Unknown tool"))
        desc = item.get("description", "")
        args = item.get("args", {})

        parts.append(f"⚠️ {tool_name} needs your approval:")
        if desc:
            parts.append(f"  {desc}")
        elif args:
            for k, v in args.items():
                parts.append(f"  • {k}: {v}")
        # Under Auto: why the reviewer asked rather than approving it.
        from row_bot.approval_messages import reviewer_note

        note = reviewer_note(item)
        if note:
            parts.append(f"  {note}")

    return "\n".join(parts)


# ── Interrupt ID extraction ──────────────────────────────────────────

def extract_interrupt_ids(data: Any) -> list[str] | None:
    """Extract ``__interrupt_id`` values from interrupt data.

    Returns every explicit ID so even a single nested interrupt resumes by
    identity instead of relying on scalar interrupt ordering.
    """
    if data is None:
        return None
    items = data if isinstance(data, list) else [data]
    ids = [
        item.get("__interrupt_id")
        for item in items
        if isinstance(item, dict) and item.get("__interrupt_id")
    ]
    return ids or None


# ── Text classification ─────────────────────────────────────────────

_YES_WORDS = frozenset({"yes", "y", "approve", "ok", "continue"})
_NO_WORDS = frozenset({"no", "n", "deny", "reject", "cancel"})


def is_approval_text(text: str) -> bool | None:
    """Classify *text* as approval, denial, or unrelated.

    Returns ``True`` for approval, ``False`` for denial,
    ``None`` if the text is not a clear yes/no.
    """
    t = text.strip().lower()
    if t in _YES_WORDS:
        return True
    if t in _NO_WORDS:
        return False
    return None


# ── Row-Bot's questions (N4) ─────────────────────────────────────────
#
# A question waits where an approval would, but is answered, never approved:
# the chat shows its options (numbered, or as Telegram buttons), the person
# replies with the number(s) or their own words, or "skip", one question at a
# time, and the turn goes on with that answer for the question alone.

_SKIP_WORDS = frozenset({"skip", "/skip"})
_NUMBERS = re.compile(r"\s*\d+(?:\s*[,\s]\s*\d+)*\s*")


def question_of(data: Any) -> dict[str, Any] | None:
    """The question an interrupt (or a workflow's pushed request) asks, if it is one."""
    from row_bot.questions import question_item

    return question_item(data)


def question_progress(pending: dict) -> dict[str, Any]:
    """Where the person is in a waiting question: its step and the answers so far (kept on the pending entry)."""
    return pending.setdefault("question_progress", {"step": 0, "answers": [], "picked": []})


def question_prompt(item: dict[str, Any], step: int = 0, *, numbered: bool = True) -> str:
    """One question of a card as plain text: the header, the question and its options, and how to answer."""
    questions = item["questions"]
    question = questions[step]
    count = f" ({step + 1} of {len(questions)})" if len(questions) > 1 else ""
    lines = [f"❓ Row-Bot has a question{count}: {question['header']}", question["question"]]
    for index, option in enumerate(question["options"], start=1):
        label = option["label"] + (" (recommended)" if option["recommended"] else "")
        lead = f"{index}. " if numbered else "• "
        lines.append(f"{lead}{label}" + (f": {option['description']}" if option["description"] else ""))
    if numbered:
        how = ("Reply with one or more numbers, like 1 3, or write your own answer."
               if question["multi_select"] else "Reply with a number, or write your own answer.")
    else:
        how = ("Tap the ones you want, then Done, or reply with your own answer."
               if question["multi_select"] else "Tap one, or reply with your own answer.")
    lines.extend(["", how + " Reply skip to skip."])
    return "\n".join(lines)


def interrupt_prompt(data: Any, approval_hint: str) -> str:
    """What a text channel sends for a waiting interrupt: the question's first step, or the approval and its hint."""
    item = question_of(data)
    if item is not None:
        return question_prompt(item)
    return f"{format_interrupt_text(data)}\n\n{approval_hint}"


def question_step(pending: dict, entry: dict[str, Any]) -> tuple[str, Any]:
    """Record one question's answer: ``("next", prompt)`` for the next one, or ``("answered", resume value)``."""
    from row_bot.questions import answer

    item = question_of(pending.get("data"))
    if item is None:
        raise ValueError("not a question")
    progress = question_progress(pending)
    progress["answers"].append(entry)
    progress["step"] += 1
    progress["picked"] = []
    if progress["step"] < len(item["questions"]):
        return "next", progress["step"]
    return "answered", answer(item, progress["answers"])


def question_reply(pending: dict, text: str, *, numbered: bool = True) -> tuple[str, Any]:
    """Take the person's reply to a waiting question.

    ``("again", prompt)`` when it isn't an answer; ``("next", prompt)`` for the next question;
    ``("answered", resume value)`` or ``("skipped", resume value)`` when the card is done.
    """
    from row_bot.questions import OTHER_CHARS, skipped

    item = question_of(pending.get("data"))
    if item is None:
        raise ValueError("not a question")
    progress = question_progress(pending)
    question = item["questions"][progress["step"]]
    reply = " ".join(str(text or "").split())
    if reply.casefold() in _SKIP_WORDS:
        return "skipped", skipped()
    count = len(question["options"])
    if numbered and _NUMBERS.fullmatch(reply):
        picks = [int(number) - 1 for number in re.findall(r"\d+", reply)]
        if (any(not 0 <= pick < count for pick in picks) or len(set(picks)) != len(picks)
                or (len(picks) > 1 and not question["multi_select"])):
            hint = (f"Reply with numbers from 1 to {count}" if question["multi_select"]
                    else f"Reply with one number from 1 to {count}")
            return "again", f"{hint}, or write your own answer. Reply skip to skip."
        entry: dict[str, Any] = {"choices": sorted(picks), "other": ""}
    elif not reply:
        return "again", question_prompt(item, progress["step"], numbered=numbered)
    elif len(reply) > OTHER_CHARS:
        return "again", f"Keep your own answer under {OTHER_CHARS} characters, or reply skip to skip."
    else:
        entry = {"choices": [], "other": reply}
    state, value = question_step(pending, entry)
    if state == "next":
        return "next", question_prompt(item, value, numbered=numbered)
    return state, value


def question_resume(data: Any, value: dict[str, Any]) -> dict[str, Any]:
    """The resume values that go on with the question's own interrupt only."""
    from row_bot.questions import resume_values

    return resume_values(data, value)


def answered_text(data: Any, value: Any) -> str:
    """What the chat shows once the card is done: "You chose: …", or that it was skipped."""
    from row_bot.questions import outcome, readable

    item = question_of(data)
    shown = readable(item, value) if item is not None else []
    if outcome(value) != "answered" or not shown:
        return "Skipped. Row-Bot goes on with its recommendation."
    if len(shown) == 1:
        return f"You chose: {shown[0]['answer']}"
    return "You chose: " + "; ".join(f"{entry['header']}: {entry['answer']}" for entry in shown)


_RESOLUTION_WORDS = {
    "approved": "✅ Approved",
    "denied": "❌ Denied",
    "answered": "✅ Answered",
    "skipped": "⏭️ Skipped",
    "timed_out": "⌛ No answer in time",
    "cancelled": "Withdrawn",
}


def resolution_words(status: str) -> str:
    """How a resolved request reads in a chat: "✅ Approved", "✅ Answered", "⏭️ Skipped"…"""
    return _RESOLUTION_WORDS.get(str(status or ""), "❌ Denied")
