"""Prefix-stability contract: each provider request is an exact prefix of the next one.

Provider prompt caches (Anthropic breakpoints, OpenAI/Gemini/xAI/DeepSeek automatic
caching, Ollama and llama.cpp KV reuse) only pay off when a request starts with the
bytes of the one before it. These cases drive the real Agent preparation path
(``_pre_model_trim`` -> ``_collect_agent_preparation_inputs`` -> ``_prepare_with_compaction``,
with provider normalization and the Anthropic cache marker) and the Chat Only path,
turn the prepared messages and bound tools into each provider's request body with that
client's own offline request builder, and assert that the earlier request's tools,
system prompt and messages are an exact prefix of the later one (``cache_control``
markers ignored). Nothing reaches the network.

Cases that break today are strict xfails naming the work package of the context and
cost plan that fixes them; that package flips its cases to passing.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Callable

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from langchain_core.utils.function_calling import convert_to_openai_tool
from langgraph.graph.message import add_messages

pytestmark = pytest.mark.subsystem

THREAD_ID = "prefix-contract"
CONTEXT_WINDOW = 200_000


# ── Bound tools ─────────────────────────────────────────────────────────────


@tool
def read_file(path: str) -> str:
    """Read a UTF-8 text file from the workspace."""
    return ""


@tool
def web_search(query: str) -> str:
    """Search the web and return the top results."""
    return ""


@tool
def browser_snapshot() -> str:
    """Capture the text of the current browser page."""
    return ""


@tool
def generate_image(prompt: str) -> str:
    """Generate an image from a prompt."""
    return ""


@tool
def generate_video(prompt: str) -> str:
    """Generate a short video clip from a prompt."""
    return ""


TOOL_GROUPS = {
    "filesystem": (read_file,),
    "web_search": (web_search,),
    "browser": (browser_snapshot,),
    "image_gen": (generate_image,),
    "video_gen": (generate_video,),
}
DEFAULT_GROUPS = ("filesystem", "web_search", "browser")


# ── Provider request shapes ─────────────────────────────────────────────────


def _bound(client: Any, tools: list) -> tuple[Any, dict]:
    if not tools:
        return client, {}
    binding = client.bind_tools(tools)
    return binding.bound, dict(binding.kwargs)


def _leading_system_split(items: list[dict]) -> dict:
    """Chat-style bodies keep the system prompt inline: its leading run is the system section."""
    head = 0
    while head < len(items) and items[head].get("role") in {"system", "developer"}:
        head += 1
    return {"system": items[:head], "messages": items[head:]}


def _anthropic_request(client: Any, messages: list, tools: list) -> dict:
    model, kwargs = _bound(client, tools)
    body = model._get_request_payload(messages, **kwargs)
    return {"tools": body.get("tools") or [], "system": body.get("system"), "messages": body["messages"]}


def _openai_chat_request(client: Any, messages: list, tools: list) -> dict:
    model, kwargs = _bound(client, tools)
    body = model._get_request_payload(messages, **kwargs)
    return {"tools": body.get("tools") or [], **_leading_system_split(body["messages"])}


def _openai_responses_request(client: Any, messages: list, tools: list) -> dict:
    model, kwargs = _bound(client, tools)
    body = model._get_request_payload(messages, **kwargs)
    split = _leading_system_split(body["input"])
    return {"tools": body.get("tools") or [], "system": [body.get("instructions"), split["system"]],
            "messages": split["messages"]}


def _codex_request(client: Any, messages: list, tools: list) -> dict:
    model, kwargs = _bound(client, tools)
    body = model._request_body(messages, **kwargs)
    return {"tools": body["tools"], "system": body["instructions"], "messages": body["input"]}


def _gemini_request(client: Any, messages: list, tools: list) -> dict:
    model, kwargs = _bound(client, tools)
    request = model._prepare_request(messages, **kwargs)
    config = request["config"].model_dump(mode="json", exclude_none=True)
    return {
        "tools": config.get("tools") or [],
        "system": config.get("system_instruction"),
        "messages": [content.model_dump(mode="json", exclude_none=True) for content in request["contents"]],
    }


def _ollama_request(client: Any, messages: list, tools: list) -> dict:
    model, kwargs = _bound(client, tools)
    body = model._chat_params(messages, **kwargs)
    return {"tools": body.get("tools") or [], **_leading_system_split(body["messages"])}


def _custom_openai_request(client: Any, messages: list, tools: list) -> dict:
    model, kwargs = _bound(client, tools)
    body = model._request_body(messages, stream=True, **kwargs)
    return {"tools": body.get("tools") or [], **_leading_system_split(body["messages"])}


def _anthropic_client(model: str, **kwargs: Any) -> Callable[[], Any]:
    def build() -> Any:
        from row_bot.providers.transports.anthropic_cancellable import CancellableChatAnthropic

        return CancellableChatAnthropic(model=model, api_key="test-key", **kwargs)
    return build


def _openai_client(model: str, **kwargs: Any) -> Callable[[], Any]:
    def build() -> Any:
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(model=model, api_key="test-key", **kwargs)
    return build


def _codex_client() -> Any:
    from row_bot.providers.transports.codex_responses import ChatCodexResponses

    return ChatCodexResponses(model_name="gpt-5.5", session_id="prefix-session", installation_id="prefix-install")


def _gemini_client() -> Any:
    from langchain_google_genai import ChatGoogleGenerativeAI

    return ChatGoogleGenerativeAI(model="gemini-2.5-flash", google_api_key="test-key")


def _ollama_client() -> Any:
    from langchain_ollama import ChatOllama

    return ChatOllama(model="llama3.1:8b", base_url="http://127.0.0.1:11434", num_ctx=CONTEXT_WINDOW)


def _custom_openai_client() -> Any:
    from row_bot.providers.transports.openai_compatible import ChatOpenAICompatible

    base_url = "http://127.0.0.1:8080/v1"
    return ChatOpenAICompatible(
        model_name="local-chat",
        base_url=base_url,
        endpoint={"provider_id": "custom_openai_lab", "display_name": "Lab", "base_url": base_url,
                  "transport": "openai_chat"},
    )


@dataclass(frozen=True)
class Shape:
    id: str
    provider_id: str
    model_id: str
    cloud: bool
    client: Callable[[], Any]
    request: Callable[[Any, list, list], dict]

    @property
    def model_ref(self) -> str:
        return f"model:{self.provider_id}:{self.model_id}"


SHAPES = (
    Shape("anthropic", "anthropic", "claude-sonnet-4-5", True,
          _anthropic_client("claude-sonnet-4-5"), _anthropic_request),
    Shape("anthropic-compatible", "minimax", "MiniMax-M2.7", True,
          _anthropic_client("MiniMax-M2.7", base_url="https://api.minimax.io/anthropic"), _anthropic_request),
    Shape("openai-chat", "openai", "gpt-4.1", True, _openai_client("gpt-4.1"), _openai_chat_request),
    Shape("openai-responses", "openai", "gpt-5.5", True,
          _openai_client("gpt-5.5", use_responses_api=True, output_version="responses/v1"),
          _openai_responses_request),
    Shape("codex", "codex", "gpt-5.5", True, _codex_client, _codex_request),
    Shape("gemini", "google", "gemini-2.5-flash", True, _gemini_client, _gemini_request),
    Shape("ollama", "ollama", "llama3.1:8b", False, _ollama_client, _ollama_request),
    Shape("custom-openai", "custom_openai_lab", "local-chat", True, _custom_openai_client, _custom_openai_request),
)
SHAPES_BY_ID = {shape.id: shape for shape in SHAPES}


# ── Prefix assertion ────────────────────────────────────────────────────────


class PrefixBroken(AssertionError):
    """The later request does not start with the earlier one."""


def _without_cache_control(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _without_cache_control(item) for key, item in value.items() if key != "cache_control"}
    if isinstance(value, list):
        return [_without_cache_control(item) for item in value]
    return value


def _canonical(request: dict) -> dict:
    return _without_cache_control(json.loads(json.dumps(request, default=str, sort_keys=True)))


def _excerpt(value: Any, limit: int = 160) -> str:
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True, default=str)
    return text if len(text) <= limit else text[:limit] + "..."


def _first_difference(before: Any, after: Any) -> str:
    if isinstance(before, list) and isinstance(after, list):
        for index, (left, right) in enumerate(zip(before, after)):
            if left != right:
                return f"[{index}] {_first_difference(left, right)}"
        shorter = min(len(before), len(after))
        extra = (before if len(before) > shorter else after)[shorter]
        side = "dropped" if len(before) > shorter else "added"
        return f"length {len(before)} -> {len(after)}; [{shorter}] {side}: {_excerpt(extra)}"
    if isinstance(before, dict) and isinstance(after, dict):
        for key in sorted(set(before) | set(after)):
            if before.get(key) != after.get(key):
                return f".{key} {_first_difference(before.get(key), after.get(key))}"
    if isinstance(before, str) and isinstance(after, str):
        offset = next((i for i, (a, b) in enumerate(zip(before, after)) if a != b), min(len(before), len(after)))
        start = max(0, offset - 40)
        return f"at char {offset}: {before[start:offset + 60]!r} -> {after[start:offset + 60]!r}"
    return f"{_excerpt(before)} -> {_excerpt(after)}"


def assert_prefix(earlier: dict, later: dict) -> None:
    """Tools and system identical, and the earlier messages an exact prefix of the later ones."""
    earlier, later = _canonical(earlier), _canonical(later)
    for section in ("tools", "system"):
        if earlier[section] != later[section]:
            raise PrefixBroken(f"{section} changed: {_first_difference(earlier[section], later[section])}")
    before, after = earlier["messages"], later["messages"]
    for index, message in enumerate(before):
        if index >= len(after):
            raise PrefixBroken(f"messages[{index}:] dropped: {len(before)} -> {len(after)} messages")
        if after[index] != message:
            raise PrefixBroken(f"messages[{index}] changed: {_first_difference(message, after[index])}")


# ── Harness over the real preparation path ──────────────────────────────────


def _clock(start: datetime) -> type[datetime]:
    """A datetime class whose now() is the test's clock (the agent's date line reads it)."""

    class Clock(datetime):
        current = start

        @classmethod
        def now(cls, tz=None):
            return cls.current if tz is None else cls.current.astimezone(tz)

    return Clock


def _memory(memory_id: str, text: str) -> dict:
    return {"id": memory_id, "entity_type": "fact", "subject": f"Memory {memory_id}", "description": text,
            "score": 0.9}


MEMORY_A = _memory("mem_a", "The user keeps project notes in notes.txt.")
MEMORY_B = _memory("mem_b", "The user prefers short bullet summaries.")


class Harness:
    """One conversation on one provider shape, through Row-Bot's real request preparation."""

    def __init__(self, agent: Any, monkeypatch: pytest.MonkeyPatch, shape: Shape, *,
                 thread_id: str = THREAD_ID) -> None:
        self.agent = agent
        self.shape = shape
        self.thread_id = thread_id
        self.monkeypatch = monkeypatch
        self.clock = _clock(datetime(2026, 10, 11, 9, 0, 5, tzinfo=timezone.utc))
        self.context_window = CONTEXT_WINDOW
        self.memories: list[dict] = []
        self.groups: tuple[str, ...] = DEFAULT_GROUPS
        self.skill_records: tuple = ()
        self._client: Any = None
        model_ref = shape.model_ref
        # Compaction is A4's contract: the window here never asks for it, so only per-call rewrites show.
        policy = SimpleNamespace(
            model_ref=model_ref, provider_id=shape.provider_id, native_limit_tokens=CONTEXT_WINDOW,
            requested_limit_tokens=None, effective_limit_tokens=CONTEXT_WINDOW,
            usable_input_tokens=1_000_000_000, compact_at_tokens=1_000_000_000,
            capacity_source="test_metadata", capacity_state="ready", limit_kind="combined",
        )
        monkeypatch.setattr(agent, "_datetime", self.clock)
        monkeypatch.setattr(agent, "get_context_size", lambda *args, **kwargs: self.context_window)
        monkeypatch.setattr(agent, "get_context_policy", lambda *args, **kwargs: policy)
        monkeypatch.setattr(agent, "get_current_model", lambda: model_ref)
        monkeypatch.setattr(agent, "is_cloud_model", lambda model: shape.cloud)
        monkeypatch.setattr(agent, "get_cloud_provider", lambda model: shape.provider_id)
        monkeypatch.setattr(agent, "is_background_workflow", lambda: False)
        monkeypatch.setattr("row_bot.self_knowledge.build_static_self_knowledge_block",
                            lambda: "SELF KNOWLEDGE: Row-Bot is a local-first desktop assistant.")
        monkeypatch.setattr("row_bot.self_knowledge.build_dynamic_self_knowledge_block",
                            lambda: "DYNAMIC STATE: two providers are configured.")
        monkeypatch.setattr("row_bot.skills.get_skills_prompt",
                            lambda *args, **kwargs: "TOOL GUIDES: read files before summarising them.")
        monkeypatch.setattr("row_bot.plugins.registry.get_skills_prompt", lambda *args, **kwargs: "")
        monkeypatch.setattr("row_bot.memory_policy.build_auto_recall", self._recall)
        monkeypatch.setattr("row_bot.memory_policy.touch_selected_memories", lambda decision: None)
        monkeypatch.setattr("row_bot.memory_policy.record_recall_trace", lambda decision, **kwargs: None)

    def _recall(self, *args: Any, **kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(allowed=bool(self.memories), reason="selected" if self.memories else "no_match",
                               candidates_seen=len(self.memories), selected=list(self.memories), trace={})

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = self.shape.client()
        return self._client

    def tick(self, seconds: int) -> None:
        self.clock.current = self.clock.current + timedelta(seconds=seconds)

    def tools(self) -> list:
        return [bound for group in self.groups for bound in TOOL_GROUPS.get(group, ())]

    def _activate(self, *, mode: str, generation_id: str) -> None:
        agent = self.agent
        agent._set_active_runtime_context(
            thread_id=self.thread_id,
            runtime_surface="normal_chat",
            requested_runtime_mode=mode,
            selected_runtime_mode=mode,
            generation_id=generation_id,
            model_override=self.shape.model_ref,
            enabled_tool_names=self.groups if mode == "agent" else (),
        )
        if mode != "agent":
            return
        from row_bot.tools.discovery import estimate_bound_tool_schema_tokens

        # What get_agent_graph records for a built graph, activated the way it activates it.
        tools = self.tools()
        cache_key = frozenset(self.groups) | {f"prefix-contract:{self.shape.id}"}
        agent._agent_cache_metadata[cache_key] = {
            "on_demand_tools": {},
            "tool_parents": tuple(sorted(self.groups)),
            "bound_schema_tokens": estimate_bound_tool_schema_tokens(tools),
            "canonical_tools": tuple(convert_to_openai_tool(bound) for bound in tools),
            "skill_records": tuple(self.skill_records),
        }
        agent._activate_agent_cache_metadata(cache_key)

    def agent_request(self, state: dict, *, turn: str) -> dict:
        """One Agent model call: the pre-model hook, then the provider's request builder."""
        self._activate(mode="agent", generation_id=turn)
        self.agent.set_active_model_override(self.shape.model_ref)
        try:
            update = self.agent._pre_model_trim(state)
        finally:
            self.agent.set_active_model_override("")
        # What LangGraph keeps from the hook: its state updates, under the state's reducers.
        if "messages" in update:
            state["messages"] = add_messages(state["messages"], update["messages"])
        if update.get("execution_budget") is not None:
            state["execution_budget"] = update["execution_budget"]
        return self.shape.request(self.client, list(update["llm_input_messages"]), self.tools())

    def chat_only_request(self, history: list[BaseMessage], text: str, *, turn: str) -> dict:
        """One Chat Only model call: the checkpoint replayed through the Chat Only preparation."""
        self._activate(mode="chat_only", generation_id=turn)
        self.monkeypatch.setattr("row_bot.threads.get_latest_checkpoint_messages", lambda thread_id: list(history))
        self.agent.set_active_model_override(self.shape.model_ref)
        try:
            inputs = self.agent._collect_chat_only_preparation_inputs(self.thread_id, text)
            prepared = self.agent._prepare_with_compaction(inputs)
        finally:
            self.agent.set_active_model_override("")
        return self.shape.request(self.client, list(prepared.messages), [])

    def new_turn(self, state: dict | None, text: str, *, turn: str) -> dict:
        messages = list((state or {}).get("messages") or [])
        return {"messages": [*messages, HumanMessage(content=text, id=f"human-{turn}")],
                "execution_budget": self.agent.new_execution_budget(turn)}


def _tool_call(call_id: str, name: str, args: dict) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"id": call_id, "name": name, "args": args, "type": "tool_call"}])


def _tool_result(call_id: str, name: str, content: str) -> ToolMessage:
    return ToolMessage(content=content, name=name, tool_call_id=call_id)


def _step(state: dict, call_id: str, name: str, args: dict, result: str) -> None:
    state["messages"] = [*state["messages"], _tool_call(call_id, name, args), _tool_result(call_id, name, result)]


def _answer(state: dict, text: str) -> None:
    state["messages"] = [*state["messages"], AIMessage(content=text)]


def _paragraphs(label: str, count: int) -> str:
    return "\n".join(f"{label} line {index}: the quarterly figures and their notes." for index in range(count))


# ── Scenarios ───────────────────────────────────────────────────────────────
# Each returns (earlier request, later request) pairs that must be prefixes.


def iteration_steady(h: Harness) -> list[tuple[dict, dict]]:
    """(a) Iterations i -> i+1 -> i+2 of one turn, within one minute, with the same recall."""
    h.memories = [MEMORY_A]
    state = h.new_turn(None, "Summarise notes.txt for me.", turn="t1")
    first = h.agent_request(state, turn="t1")
    _step(state, "call_1", "read_file", {"path": "notes.txt"}, _paragraphs("notes", 6))
    second = h.agent_request(state, turn="t1")
    _step(state, "call_2", "web_search", {"query": "quarterly figures"}, _paragraphs("result", 4))
    third = h.agent_request(state, turn="t1")
    return [(first, second), (second, third)]


def iteration_across_minute(h: Harness) -> list[tuple[dict, dict]]:
    """(a) An iteration whose call lands in the next minute (F1)."""
    h.memories = [MEMORY_A]
    h.tick(50)
    state = h.new_turn(None, "Summarise notes.txt for me.", turn="t1")
    first = h.agent_request(state, turn="t1")
    _step(state, "call_1", "read_file", {"path": "notes.txt"}, _paragraphs("notes", 6))
    h.tick(20)
    return [(first, h.agent_request(state, turn="t1"))]


def iteration_after_memory_save(h: Harness) -> list[tuple[dict, dict]]:
    """(a) Recall re-runs every iteration; a memory saved mid-turn changes it (F3)."""
    h.memories = [MEMORY_A]
    state = h.new_turn(None, "Remember that I like bullet summaries, then summarise notes.txt.", turn="t1")
    first = h.agent_request(state, turn="t1")
    _step(state, "call_1", "read_file", {"path": "notes.txt"}, _paragraphs("notes", 6))
    h.memories = [MEMORY_A, MEMORY_B]
    return [(first, h.agent_request(state, turn="t1"))]


def iteration_after_skill_load(h: Harness) -> list[tuple[dict, dict]]:
    """(a) skill_load mid-turn rewrites the stable skills section (F5)."""
    from row_bot.skill_discovery import SkillRecord
    from row_bot.skills_activation import load_auto_skill

    record = SkillRecord(canonical_id="report_writer", alias=None, display_name="Report writer", icon="R",
                         description="Writes short reports.", tags=(), activation={},
                         instructions="1. Read the sources. 2. Write three bullets.", source="manual", root=None)
    h.skill_records = (record,)
    state = h.new_turn(None, "Write a short report on notes.txt.", turn="t1")
    first = h.agent_request(state, turn="t1")
    _step(state, "call_1", "skill_load", {"name": "report_writer"}, '{"loaded": "report_writer"}')
    load_auto_skill(h.thread_id, "report_writer")
    return [(first, h.agent_request(state, turn="t1"))]


def turn_steady(h: Harness) -> list[tuple[dict, dict]]:
    """(b) Turn N's last call -> turn N+1's first call, same minute, nothing recalled."""
    state = h.new_turn(None, "Summarise notes.txt for me.", turn="t1")
    _step(state, "call_1", "read_file", {"path": "notes.txt"}, _paragraphs("notes", 6))
    last = h.agent_request(state, turn="t1")
    _answer(state, "Here are the notes in three bullets.")
    state = h.new_turn(state, "Now compare them with last quarter.", turn="t2")
    return [(last, h.agent_request(state, turn="t2"))]


def turn_same_recall(h: Harness) -> list[tuple[dict, dict]]:
    """(b) Turn N -> N+1 in the same minute with the same recall: the block moves to the new message (F3)."""
    h.memories = [MEMORY_A]
    state = h.new_turn(None, "Summarise notes.txt for me.", turn="t1")
    _step(state, "call_1", "read_file", {"path": "notes.txt"}, _paragraphs("notes", 6))
    last = h.agent_request(state, turn="t1")
    _answer(state, "Here are the notes in three bullets.")
    state = h.new_turn(state, "Now compare them with last quarter.", turn="t2")
    return [(last, h.agent_request(state, turn="t2"))]


def turn_across_minute_new_recall(h: Harness) -> list[tuple[dict, dict]]:
    """(b) Turn N's last call -> turn N+1's first call, a minute later, with a different recall (F1, F3)."""
    h.memories = [MEMORY_A]
    h.tick(30)
    state = h.new_turn(None, "Summarise notes.txt for me.", turn="t1")
    _step(state, "call_1", "read_file", {"path": "notes.txt"}, _paragraphs("notes", 6))
    last = h.agent_request(state, turn="t1")
    _answer(state, "Here are the notes in three bullets.")
    h.tick(45)
    h.memories = [MEMORY_B]
    state = h.new_turn(state, "Now compare them with last quarter.", turn="t2")
    return [(last, h.agent_request(state, turn="t2"))]


def turn_masks_old_tool_output(h: Harness) -> list[tuple[dict, dict]]:
    """(c) The sixth turn summarises the first turn's tool output (rolling 5-turn masking, F19)."""
    state = None
    for number in range(1, 6):
        state = h.new_turn(state, f"Read part {number} of the report.", turn=f"t{number}")
        _step(state, f"call_{number}", "read_file", {"path": f"part{number}.txt"},
              _paragraphs(f"part {number}", 30))
        if number < 5:
            _answer(state, f"Part {number} read.")
    last = h.agent_request(state, turn="t5")
    _answer(state, "Part 5 read.")
    state = h.new_turn(state, "Now summarise all five parts.", turn="t6")
    return [(last, h.agent_request(state, turn="t6"))]


def iteration_shrinks_tool_outputs(h: Harness) -> list[tuple[dict, dict]]:
    """(c) The third large result pushes tool output past its budget; every result is cut again (F20)."""
    # A 40K window leaves tool output about 22K tokens; each export is about 8K (distinct, so none is a duplicate).
    h.context_window = 40_000
    state = h.new_turn(None, "Read the three exports and reconcile them.", turn="t1")
    for number in (1, 2):
        _step(state, f"call_{number}", "read_file", {"path": f"export{number}.csv"},
              _paragraphs(f"export {number}", 550))
    before = h.agent_request(state, turn="t1")
    _step(state, "call_3", "read_file", {"path": "export3.csv"}, _paragraphs("export 3", 550))
    return [(before, h.agent_request(state, turn="t1"))]


def iteration_stubs_old_browser_snapshot(h: Harness) -> list[tuple[dict, dict]]:
    """(c) A sixth browser snapshot turns the oldest one into a stub (F21)."""
    state = h.new_turn(None, "Walk through the six docs pages.", turn="t1")
    for number in range(1, 6):
        _step(state, f"call_{number}", "browser_snapshot", {},
              f"URL: https://docs.example.test/page-{number}\nTitle: Page {number}\n" + _paragraphs("dom", 8))
    before = h.agent_request(state, turn="t1")
    _step(state, "call_6", "browser_snapshot", {},
          "URL: https://docs.example.test/page-6\nTitle: Page 6\n" + _paragraphs("dom", 8))
    return [(before, h.agent_request(state, turn="t1"))]


def iteration_dedups_repeated_tool_output(h: Harness) -> list[tuple[dict, dict]]:
    """(c) A repeated identical result replaces the earlier copy with a note (F21)."""
    state = h.new_turn(None, "Search for the quarterly figures.", turn="t1")
    _step(state, "call_1", "web_search", {"query": "quarterly figures"}, _paragraphs("result", 8))
    before = h.agent_request(state, turn="t1")
    _step(state, "call_2", "web_search", {"query": "quarterly figures 2026"}, _paragraphs("result", 8))
    return [(before, h.agent_request(state, turn="t1"))]


def turn_media_request_changes_tools(h: Harness) -> list[tuple[dict, dict]]:
    """(d) A media keyword narrows the bound tool set for one turn (F38)."""
    from row_bot.application.conversation_creation import media_tool_selection

    enabled = ["filesystem", "web_search", "image_gen", "video_gen"]
    h.groups = tuple(enabled)
    state = h.new_turn(None, "Summarise notes.txt for me.", turn="t1")
    _step(state, "call_1", "read_file", {"path": "notes.txt"}, _paragraphs("notes", 6))
    last = h.agent_request(state, turn="t1")
    _answer(state, "Here are the notes in three bullets.")
    text = "Make an image of a lighthouse for the cover."
    h.groups = tuple(media_tool_selection(text, enabled, has_design=False))
    state = h.new_turn(state, text, turn="t2")
    return [(last, h.agent_request(state, turn="t2"))]


def chat_only_turn(h: Harness) -> list[tuple[dict, dict]]:
    """Chat Only: turn N -> turn N+1 a minute later with a different recall."""
    h.memories = [MEMORY_A]
    h.tick(30)
    first = h.chat_only_request([], "What is a prompt cache?", turn="t1")
    history = [HumanMessage(content="What is a prompt cache?", id="human-t1"),
               AIMessage(content="It reuses the start of an earlier request.", id="ai-t1")]
    h.tick(45)
    h.memories = [MEMORY_B]
    return [(first, h.chat_only_request(history, "And why does order matter?", turn="t2"))]


SCENARIOS: dict[str, Callable[[Harness], list[tuple[dict, dict]]]] = {
    "iteration_steady": iteration_steady,
    "iteration_across_minute": iteration_across_minute,
    "iteration_after_memory_save": iteration_after_memory_save,
    "iteration_after_skill_load": iteration_after_skill_load,
    "turn_steady": turn_steady,
    "turn_same_recall": turn_same_recall,
    "turn_across_minute_new_recall": turn_across_minute_new_recall,
    "turn_masks_old_tool_output": turn_masks_old_tool_output,
    "iteration_shrinks_tool_outputs": iteration_shrinks_tool_outputs,
    "iteration_stubs_old_browser_snapshot": iteration_stubs_old_browser_snapshot,
    "iteration_dedups_repeated_tool_output": iteration_dedups_repeated_tool_output,
    "turn_media_request_changes_tools": turn_media_request_changes_tools,
    "chat_only_turn": chat_only_turn,
}

# Today's breaks, by the work package of the context and cost plan that fixes them. A package
# that fixes a case removes its entry here; strict xfail fails the run if a fixed case stays marked.
ALL_SHAPES = "*"
_RECALL_MOVES = "fixed by WP A2: the recall block moves from the last message to the newest one (F3)"
KNOWN_BREAKS: dict[str, dict[str, str]] = {
    "iteration_across_minute": {
        ALL_SHAPES: "fixed by WP A2: the minute-resolution date line is a system section ahead of history (F1)",
    },
    "iteration_after_memory_save": {
        ALL_SHAPES: "fixed by WP A2: recall re-runs every iteration and sits in or before the system prompt (F3)",
    },
    "iteration_after_skill_load": {
        ALL_SHAPES: "fixed by WP A2: skill_load rewrites the stable skills section mid-turn (F5)",
    },
    "turn_same_recall": {
        "openai-chat": _RECALL_MOVES,
        "openai-responses": _RECALL_MOVES,
        "ollama": _RECALL_MOVES,
    },
    "turn_across_minute_new_recall": {
        ALL_SHAPES: "fixed by WP A2: the date line and recall change ahead of history (F1, F3)",
    },
    "turn_masks_old_tool_output": {
        ALL_SHAPES: "fixed by WP A4: rolling 5-turn masking rewrites an old tool result (F19)",
    },
    "iteration_shrinks_tool_outputs": {
        ALL_SHAPES: "fixed by WP A4: proportional shrink rewrites every earlier tool result (F20)",
    },
    "iteration_stubs_old_browser_snapshot": {
        ALL_SHAPES: "fixed by WP A4: browser stubbing rewrites an earlier snapshot (F21)",
    },
    "iteration_dedups_repeated_tool_output": {
        ALL_SHAPES: "fixed by WP A4: dedup rewrites the earlier copy of a repeated result (F21)",
    },
    "turn_media_request_changes_tools": {
        ALL_SHAPES: "fixed by WP A5: a media keyword changes the bound tool set for one turn (F38)",
    },
}


def _cases():
    for scenario in SCENARIOS:
        for shape in SHAPES:
            breaks = KNOWN_BREAKS.get(scenario, {})
            reason = breaks.get(shape.id) or breaks.get(ALL_SHAPES)
            marks = [pytest.mark.xfail(strict=True, raises=PrefixBroken, reason=reason)] if reason else []
            yield pytest.param(scenario, shape, id=f"{scenario}-{shape.id}", marks=marks)


@pytest.fixture
def agent_module(tmp_path, reload_for_data_dir):
    # Skill activation state is kept per data folder (skill_load writes it).
    agent, _activation = reload_for_data_dir(tmp_path / "data", "row_bot.agent", "row_bot.skills_activation")
    return agent


@pytest.mark.parametrize(("scenario", "shape"), list(_cases()))
def test_each_request_is_a_prefix_of_the_next(agent_module, monkeypatch, scenario: str, shape: Shape) -> None:
    harness = Harness(agent_module, monkeypatch, shape)

    for earlier, later in SCENARIOS[scenario](harness):
        assert_prefix(earlier, later)


# ── DEBUG prefix diff ───────────────────────────────────────────────────────


def test_debug_prefix_diff_names_the_first_change_without_prompt_text(agent_module, monkeypatch, caplog) -> None:
    from row_bot import prompt_prefix_diff

    fingerprinted: list[int] = []
    fingerprint = prompt_prefix_diff.fingerprint_request
    monkeypatch.setattr(prompt_prefix_diff, "fingerprint_request",
                        lambda *args, **kwargs: fingerprinted.append(1) or fingerprint(*args, **kwargs))
    harness = Harness(agent_module, monkeypatch, SHAPES_BY_ID["openai-chat"], thread_id="prefix-diff-log")
    harness.memories = [MEMORY_A]
    state = harness.new_turn(None, "Summarise notes.txt for me.", turn="t1")

    def diff_lines() -> list[str]:
        return [record.getMessage() for record in caplog.records if record.name == prompt_prefix_diff.logger.name]

    caplog.set_level(logging.INFO, logger=prompt_prefix_diff.logger.name)
    harness.agent_request(state, turn="t1")
    assert fingerprinted == [] and diff_lines() == []  # off unless DEBUG: nothing hashed or kept

    caplog.set_level(logging.DEBUG, logger=prompt_prefix_diff.logger.name)
    harness.agent_request(state, turn="t1")
    _step(state, "call_1", "read_file", {"path": "notes.txt"}, _paragraphs("notes", 6))
    harness.agent_request(state, turn="t1")
    harness.tick(60)
    _step(state, "call_2", "web_search", {"query": "quarterly figures"}, _paragraphs("result", 4))
    harness.agent_request(state, turn="t1")

    lines = diff_lines()
    assert len(lines) == 3
    assert "first request" in lines[0] and "turn.date_time=" in lines[0]
    assert "stable prefix" in lines[1] and "+2 appended" in lines[1]
    assert "prefix broken at message 4: previous=system:turn.date_time" in lines[2]
    assert "changed sections=[turn.date_time]" in lines[2]
    for line in lines:
        assert "thread=prefix-diff-log" in line
        for text in ("Summarise notes.txt", "Current date and time", "project notes", "quarterly figures"):
            assert text not in line


def test_debug_prefix_diff_keeps_the_most_recent_threads_only() -> None:
    from row_bot.prompt_prefix_diff import PrefixTracker, fingerprint_request

    tracker = PrefixTracker(max_threads=2)
    request = fingerprint_request([HumanMessage(content="hello")], [])
    for thread in ("a", "b", "c"):
        tracker.observe(thread, request)

    assert len(tracker) == 2
    assert tracker.observe("a", request).startswith("first request")  # evicted, so seen afresh
    assert tracker.observe("c", request).startswith("stable prefix")
