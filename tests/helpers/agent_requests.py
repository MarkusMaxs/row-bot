"""Drive the Agent's real request preparation (``_pre_model_trim``) over a scripted conversation.

The model, its context policy, Row-Bot's state and recall are fixed, so a test sees exactly the
messages the provider would be sent for each model call. Nothing reaches a provider.
"""

from __future__ import annotations

import contextvars
import json
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langgraph.graph.message import add_messages

MODEL_REF = "model:openai:gpt-4.1"
UNLIMITED = 1_000_000_000


def _clock(start: datetime) -> type[datetime]:
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return start if tz is None else start.astimezone(tz)

    return Clock


def text_of_tokens(label: str, tokens: int) -> str:
    """Distinct text of about *tokens* approximate tokens (four characters each)."""
    line = f"{label}: the quarterly figures and their notes, line "
    lines: list[str] = []
    size = 0
    index = 0
    while size < tokens * 4:
        lines.append(f"{line}{index}.")
        size += len(lines[-1]) + 1
        index += 1
    return "\n".join(lines)


def wire(messages: list[BaseMessage]) -> list[tuple[str, str, str]]:
    """What a provider request is built from: role, content and tool call id, in order."""
    return [
        (
            str(message.type),
            json.dumps(message.content, sort_keys=True, ensure_ascii=False, default=str),
            str(getattr(message, "tool_call_id", "") or ""),
        )
        for message in messages
    ]


def is_prefix(earlier: list[BaseMessage], later: list[BaseMessage]) -> bool:
    before, after = wire(earlier), wire(later)
    return after[: len(before)] == before


def tool_contents(messages: list[BaseMessage]) -> dict[str, str]:
    """The content of each tool result the model sees, by tool call id."""
    return {
        str(message.tool_call_id): str(message.content)
        for message in messages
        if isinstance(message, ToolMessage)
    }


class UnavailableSummarizer:
    """The compaction model: records each request and fails, as a summarizer that can't be reached.

    Compaction never builds a provider client here: with a key left in the environment (or a client
    cached) by another test, that client called the real provider (PR #438).
    """

    def __init__(self) -> None:
        self.requests: list[list[BaseMessage]] = []

    def invoke(self, messages: list[BaseMessage], *args: Any, **kwargs: Any) -> AIMessage:
        self.requests.append(list(messages))
        raise RuntimeError("This test has no summarizer.")


class AgentConversation:
    """One thread on a fixed OpenAI-chat model, through the real Agent preparation path."""

    def __init__(
        self,
        agent: Any,
        monkeypatch: pytest.MonkeyPatch,
        *,
        context_window: int = 200_000,
        compact_at: int = UNLIMITED,
        usable: int = UNLIMITED,
        thread_id: str = "",
        create_thread: bool = True,
    ) -> None:
        self.agent = agent
        self.summarizer = UnavailableSummarizer()
        self.thread_id = thread_id or f"agent-requests-{uuid.uuid4().hex[:10]}"
        if create_thread:
            from row_bot import threads

            threads.create_thread("Agent requests", thread_id=self.thread_id, seed_default_skills=False)
        self.policy = SimpleNamespace(
            model_ref=MODEL_REF, provider_id="openai", native_limit_tokens=context_window,
            requested_limit_tokens=None, effective_limit_tokens=context_window, usable_input_tokens=usable,
            compact_at_tokens=compact_at, capacity_source="test_metadata", capacity_state="ready",
            limit_kind="combined",
        )
        self.state: dict[str, Any] = {"messages": []}
        self.turns = 0
        self.calls = 0
        monkeypatch.setattr(agent, "_datetime", _clock(datetime(2026, 10, 11, 9, 0, 5, tzinfo=timezone.utc)))
        monkeypatch.setattr(agent, "get_context_size", lambda *args, **kwargs: context_window)
        monkeypatch.setattr(agent, "get_context_policy", lambda *args, **kwargs: self.policy)
        monkeypatch.setattr(agent, "get_current_model", lambda: MODEL_REF)
        monkeypatch.setattr(agent, "is_cloud_model", lambda model: True)
        monkeypatch.setattr(agent, "get_cloud_provider", lambda model: "openai")
        monkeypatch.setattr(agent, "is_background_workflow", lambda: False)
        monkeypatch.setattr(agent, "_compaction_model", lambda inputs, output_tokens: self.summarizer)
        monkeypatch.setattr("row_bot.self_knowledge.build_static_self_knowledge_block", lambda: "SELF KNOWLEDGE")
        monkeypatch.setattr("row_bot.self_knowledge.build_dynamic_self_knowledge_block", lambda: "STATE")
        monkeypatch.setattr("row_bot.skills.get_skills_prompt", lambda *args, **kwargs: "")
        monkeypatch.setattr("row_bot.plugins.registry.get_skills_prompt", lambda *args, **kwargs: "")
        monkeypatch.setattr(
            "row_bot.memory_policy.build_auto_recall",
            lambda *args, **kwargs: SimpleNamespace(
                allowed=False, reason="no_match", candidates_seen=0, selected=[], trace={},
            ),
        )
        monkeypatch.setattr("row_bot.memory_policy.touch_selected_memories", lambda decision: None)
        monkeypatch.setattr("row_bot.memory_policy.record_recall_trace", lambda decision, **kwargs: None)

    def say(self, text: str) -> None:
        """A new person turn."""
        self.turns += 1
        self.state = {
            "messages": [*self.state["messages"], HumanMessage(content=text, id=f"person-{self.turns}")],
            "execution_budget": self.agent.new_execution_budget(f"{self.thread_id}-turn-{self.turns}"),
        }

    def tool(self, name: str, content: Any, **fields: Any) -> str:
        """The model calls *name*; its result (with any extra ToolMessage *fields*) is stored."""
        self.calls += 1
        call_id = f"call_{self.calls}"
        self.state["messages"] = [
            *self.state["messages"],
            AIMessage(content="", id=f"ai-{self.calls}",
                      tool_calls=[{"id": call_id, "name": name, "args": {}, "type": "tool_call"}]),
            ToolMessage(content=content, name=name, tool_call_id=call_id,
                        **({"id": f"tool-{self.calls}"} | fields)),
        ]
        return call_id

    def answer(self, text: str) -> None:
        self.state["messages"] = [*self.state["messages"], AIMessage(content=text, id=f"answer-{self.turns}")]

    def request(self) -> list[BaseMessage]:
        """One model call: the messages the provider request is built from."""

        def prepare() -> dict:
            self.agent._set_active_runtime_context(
                thread_id=self.thread_id,
                runtime_surface="normal_chat",
                requested_runtime_mode="agent",
                selected_runtime_mode="agent",
                generation_id=f"{self.thread_id}-turn-{self.turns}",
                model_override=MODEL_REF,
                enabled_tool_names=("filesystem",),
            )
            return self.agent._pre_model_trim(self.state)

        update = contextvars.copy_context().run(prepare)
        # What LangGraph keeps from the hook: its state updates, under the state's reducers.
        if "messages" in update:
            self.state["messages"] = add_messages(self.state["messages"], update["messages"])
        if update.get("execution_budget") is not None:
            self.state["execution_budget"] = update["execution_budget"]
        return list(update["llm_input_messages"])
