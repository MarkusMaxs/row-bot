"""UI-neutral context packing helpers for Agent runs."""

from __future__ import annotations

from typing import Any, Callable, Iterable, Mapping


_CONTEXT_MODES = {"auto", "focused", "recent", "full", "empty", "resume"}


class AgentContextError(ValueError):
    """Raised when an Agent context mode or packet is invalid."""


def normalize_context_mode(mode: str | None, *, fallback: str = "focused") -> str:
    value = str(mode or "").strip().lower()
    if not value:
        value = fallback
    if value not in _CONTEXT_MODES:
        raise AgentContextError(f"Invalid Agent context mode: {mode}")
    return value


def profile_context_mode(profile_snapshot: Mapping[str, Any] | None) -> str:
    profile = dict(profile_snapshot or {})
    context_policy = profile.get("context_policy_json") or {}
    if not isinstance(context_policy, dict):
        return "focused"
    mode = normalize_context_mode(
        context_policy.get("default_context_mode"),
        fallback="focused",
    )
    return "focused" if mode == "auto" else mode


def profile_context_budget(profile_snapshot: Mapping[str, Any] | None) -> int:
    profile = dict(profile_snapshot or {})
    context_policy = profile.get("context_policy_json") or {}
    if not isinstance(context_policy, dict):
        return 4000
    try:
        value = int(context_policy.get("max_context_tokens") or 0)
    except (TypeError, ValueError):
        value = 0
    return value if value > 0 else 4000


def select_context_mode(
    requested_mode: str | None,
    profile_snapshot: Mapping[str, Any] | None,
) -> str:
    requested = normalize_context_mode(
        requested_mode,
        fallback=profile_context_mode(profile_snapshot),
    )
    if requested == "auto":
        return profile_context_mode(profile_snapshot)
    return requested


def estimate_tokens(text: str) -> int:
    return max(1, (len(str(text or "")) + 3) // 4)


def message_to_text(message: Any) -> str:
    """Convert common checkpoint message shapes into compact transcript text."""
    from row_bot.turn_context import is_turn_context_message

    if is_turn_context_message(message):
        # Row-Bot's per-turn context for the parent's model, not part of its conversation.
        return ""
    role = ""
    content: Any = ""
    if isinstance(message, dict):
        role = str(message.get("role") or message.get("type") or "")
        content = message.get("content", "")
    elif isinstance(message, (tuple, list)) and len(message) >= 2:
        role = str(message[0] or "")
        content = message[1]
    else:
        role = str(getattr(message, "type", "") or getattr(message, "role", "") or "")
        content = getattr(message, "content", "")
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text") or item.get("content") or ""
                if text:
                    parts.append(str(text))
            elif item:
                parts.append(str(item))
        content = "\n".join(parts)
    text = " ".join(str(content or "").split())
    if not text:
        return ""
    role = {
        "human": "User",
        "user": "User",
        "ai": "Assistant",
        "assistant": "Assistant",
        "system": "System",
        "tool": "Tool",
    }.get(role.lower(), role.title() if role else "Message")
    return f"{role}: {text}"


def transcript_to_text(messages: Iterable[Any], *, limit: int | None = None) -> str:
    items = [line for msg in messages for line in [message_to_text(msg)] if line]
    if limit is not None and limit >= 0:
        items = items[-limit:]
    return "\n".join(items)


def load_parent_context(parent_thread_id: str, *, recent_limit: int = 8) -> dict[str, Any]:
    if not parent_thread_id:
        return {"summary": "", "recent": "", "full": "", "message_count": 0}
    try:
        from row_bot.threads import get_latest_checkpoint_messages, load_validated_summary_state

        messages = get_latest_checkpoint_messages(parent_thread_id)
        summary_payload = (
            load_validated_summary_state(parent_thread_id, "agent", messages=messages)
            or load_validated_summary_state(parent_thread_id, "chat_only", messages=messages)
            or {}
        )
    except Exception:
        messages = []
        summary_payload = {}
    from row_bot.turn_context import without_turn_contexts

    messages = without_turn_contexts(messages)
    summary_text = str(summary_payload.get("summary") or "").strip()
    summary = (
        '<HISTORICAL_CONTEXT untrusted="true">\n'
        + summary_text
        + "\n</HISTORICAL_CONTEXT>"
        if summary_text
        else ""
    )
    return {
        "summary": summary,
        "recent": transcript_to_text(messages, limit=recent_limit),
        "full": transcript_to_text(messages),
        "message_count": len(messages),
    }


def _profile_header(profile: Mapping[str, Any]) -> list[str]:
    # The profile's instructions and handoff contract are in the child's system prompt (the
    # ``agent.profile`` section, from the profile snapshot its run carries), so the packet only
    # names the profile instead of repeating them (F6c).
    display_name = str(profile.get("display_name") or profile.get("slug") or "Agent")
    parts = [f"AGENT PROFILE: {display_name}"]
    if str(profile.get("instructions") or "").strip() or str(profile.get("handoff_contract") or "").strip():
        parts.append("Follow this profile's instructions and handoff contract in your system prompt.")
    return parts


# The newest parent turn, cut to fit, is kept only when at least this much of it fits.
_MIN_PARTIAL_TURN_CHARS = 200


def _recent_parent_sections(
    compose: Callable[[list[tuple[str, str]]], str],
    parent_context: Mapping[str, Any],
    context: str,
    budget: int,
) -> list[tuple[str, str]]:
    """PARENT SUMMARY, RECENT PARENT TURNS and CALLER CONTEXT within the child's allowance.

    Over the allowance the oldest parent turns go first, then the parent
    summary, then the start of the newest turn. The context the caller wrote
    for the child is kept (WP C3, F52).
    """
    summary = [("PARENT SUMMARY", str(parent_context.get("summary") or ""))]
    summary = summary if summary[0][1] else []
    caller = [("CALLER CONTEXT", context)] if context else []
    # One line per message: ``message_to_text`` folds each message onto one line.
    turns = [line for line in str(parent_context.get("recent") or "").split("\n") if line]

    def sections(kept: list[str]) -> list[tuple[str, str]]:
        recent = [("RECENT PARENT TURNS", "\n".join(kept))] if kept else []
        return [*summary, *recent, *caller]

    def fits(kept: list[str]) -> bool:
        return estimate_tokens(compose(sections(kept))) <= budget

    while len(turns) > 1 and not fits(turns):
        turns.pop(0)
    if not fits(turns):
        summary = []
    if turns and not fits(turns):
        # The newest turn alone is too long: keep its end, when a useful part fits.
        newest = turns[0]
        room = (budget - estimate_tokens(compose(sections([])))) * 4 - 48
        turns = ["..." + newest[-room:]] if room >= _MIN_PARTIAL_TURN_CHARS else []
        while turns and not fits(turns) and len(turns[0]) > _MIN_PARTIAL_TURN_CHARS:
            turns = ["..." + turns[0][19:]]
        if turns and not fits(turns):
            turns = []
    return sections(turns)


def build_child_agent_prompt(
    *,
    objective: str,
    profile_snapshot: Mapping[str, Any],
    context: str = "",
    context_mode: str = "",
    parent_thread_id: str = "",
    parent_run_id: str = "",
    model_override: str = "",
) -> dict[str, str]:
    """Build the focused prompt packet for a child Agent run.

    V1 starts with a deliberately compact packet: profile instructions,
    mission, optional caller-supplied context, and the expected handoff shape.
    Transcript-derived `recent`/`full` modes are layered in the later context
    packer phase.
    """
    profile = dict(profile_snapshot or {})
    requested_mode = normalize_context_mode(context_mode, fallback="auto")
    mode = select_context_mode(context_mode, profile)
    if requested_mode == "auto":
        parent_preview = load_parent_context(parent_thread_id, recent_limit=2)
        mode = "focused" if context else "recent" if parent_preview["message_count"] else profile_context_mode(profile)
    objective = str(objective or "").strip()
    context = str(context or "").strip()
    parent_context = load_parent_context(parent_thread_id)
    budget = profile_context_budget(profile)
    fallback = ""
    model_ref = str(model_override or "").strip()
    runtime_lines = [
        "The parent selected this child Agent's runtime model before start.",
        (
            f"Runtime model override: {model_ref}"
            if model_ref
            else "Runtime model override: inherited from the parent/default runtime."
        ),
        "Do not call row_bot_update_setting(setting='model') to satisfy your own runtime model request. "
        "Do not use setting='thread_model' or setting='default_model' for that either.",
    ]

    def compose(context_sections: list[tuple[str, str]]) -> str:
        parts = [
            *_profile_header(profile),
            "",
            "MISSION:",
            objective,
        ]
        parts.extend(["", "RUNTIME MODEL:", "\n".join(runtime_lines)])
        for title, body in context_sections:
            if body:
                parts.extend(["", f"{title}:", body])
        if fallback:
            parts.extend(["", "CONTEXT FALLBACK:", fallback])
        if parent_thread_id or parent_run_id:
            refs = []
            if parent_thread_id:
                refs.append(f"parent_thread_id={parent_thread_id}")
            if parent_run_id:
                refs.append(f"parent_run_id={parent_run_id}")
            parts.extend(["", "PARENT REFS:", ", ".join(refs)])
        parts.extend([
            "",
            "Return a concise result for the parent agent. Include evidence, files or commands inspected, risks, and next steps when relevant.",
        ])
        return "\n".join(parts).strip()

    context_sections: list[tuple[str, str]] = []
    if mode == "focused":
        if context:
            context_sections.append(("CONTEXT PACKET", context))
    elif mode == "empty":
        context_sections.append(("CONTEXT MODE", "empty: no parent transcript or caller context was included."))
    elif mode == "recent":
        context_sections = _recent_parent_sections(compose, parent_context, context, budget)
    elif mode == "full":
        full_text = parent_context["full"]
        if context:
            full_text = (full_text + "\n\nCALLER CONTEXT:\n" + context).strip()
        if estimate_tokens(full_text) > budget:
            fallback = "full_to_recent_summary"
            mode = "recent"
            context_sections = _recent_parent_sections(compose, parent_context, context, budget)
        elif full_text:
            context_sections.append(("FULL PARENT TRANSCRIPT", full_text))
    elif mode == "resume":
        if context:
            context_sections.append(("RESUME CONTEXT", context))
        context_sections.append(("CONTEXT MODE", "resume: continue an existing child thread with the supplied instruction."))

    prompt = compose(context_sections)
    if estimate_tokens(prompt) > budget:
        # Context sections are atomic; removing a complete historical packet
        # cannot leave a tool call separated from its result. Mission, profile,
        # runtime restrictions and handoff remain mandatory.
        for title, body in reversed(context_sections):
            if estimate_tokens(prompt) <= budget:
                break
            prompt = prompt.replace(f"\n{title}:\n{body}", "", 1)
        if estimate_tokens(prompt) > budget:
            raise AgentContextError("The mandatory child packet exceeds its configured context allowance.")
    return {
        "mode": mode,
        "prompt": prompt,
        "summary": (
            f"{profile.get('display_name') or profile.get('slug') or 'Agent'} using {mode} context"
            + (f" fallback={fallback}" if fallback else "")
            + (" with supplied context" if context and mode != "empty" else "")
        ),
        "fallback": fallback,
        "message_count": str(parent_context["message_count"]),
        "estimated_tokens": str(estimate_tokens(prompt)),
    }
