"""Prompt-cache breakpoints on Claude routes (WP A3).

The direct Claude API gets a breakpoint on the last stable system block and
top-level automatic caching for the conversation tail, with a lifetime that
follows the conversation (one hour when a person is in it, five minutes for
back-to-back loops). OpenRouter's Claude models get the same documented forms;
Requesty's Claude routes get Requesty's own auto-cache flag. Routes nobody has
checked get nothing. Fake Messages, OpenRouter and Requesty APIs answer through
``httpx.MockTransport``, so the real request builders run with no network.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from row_bot.agent_budget import new_execution_budget
from row_bot.prompt_cache import (
    CONVERSATION_CACHE_KEY,
    apply_anthropic_system_cache_marker,
    with_conversation_cache,
)
from row_bot.providers.transports import anthropic_cancellable, openrouter_cancellable
from row_bot.providers.transports import prompt_cache_breakpoints as cache_breakpoints
from row_bot.providers.transports.anthropic_cancellable import CancellableChatAnthropic
from row_bot.providers.transports.openai_compatible import ChatOpenAICompatible
from row_bot.providers.transports.openrouter_cancellable import CancellableChatOpenRouter

pytestmark = pytest.mark.subsystem

ONE_HOUR = {"type": "ephemeral", "ttl": "1h"}
FIVE_MINUTES = {"type": "ephemeral"}


@pytest.fixture(autouse=True)
def _fresh_cache_memory(monkeypatch):
    from collections import OrderedDict

    monkeypatch.setattr(cache_breakpoints, "_BREAKPOINT_DIGESTS", OrderedDict())
    monkeypatch.setattr(cache_breakpoints, "_REJECTED_ROUTES", set())


def _fresh_agent(tmp_path, monkeypatch, reload_for_data_dir):

    return reload_for_data_dir(tmp_path / "data", "row_bot.agent")[0]


def _has_cache_control(message) -> bool:
    content = getattr(message, "content", "")
    return isinstance(content, list) and any(
        isinstance(block, dict) and "cache_control" in block
        for block in content
    )


def _markers(value: Any) -> list[dict]:
    """Every ``cache_control`` value in a request body, wherever it sits."""
    found: list[dict] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "cache_control" and isinstance(item, dict):
                found.append(item)
            else:
                found.extend(_markers(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(_markers(item))
    return found


def _message_markers(body: dict) -> list[tuple[int, int]]:
    return [
        (message_index, block_index)
        for message_index, message in enumerate(body.get("messages") or [])
        if isinstance(message.get("content"), list)
        for block_index, block in enumerate(message["content"])
        if isinstance(block, dict) and "cache_control" in block
    ]


def _quiet_agent(agent, monkeypatch, provider_id: str, model_id: str) -> str:
    model_ref = f"model:{provider_id}:{model_id}"
    decision = SimpleNamespace(allowed=False, reason="disabled", candidates_seen=0, selected=[], trace={})
    monkeypatch.setattr(agent, "get_context_size", lambda *args: 200_000)
    monkeypatch.setattr(agent, "get_current_model", lambda: model_ref)
    monkeypatch.setattr(agent, "is_cloud_model", lambda model: True)
    monkeypatch.setattr(agent, "get_cloud_provider", lambda model: provider_id)
    monkeypatch.setattr(agent, "is_background_workflow", lambda: False)
    monkeypatch.setattr("row_bot.self_knowledge.build_static_self_knowledge_block", lambda: "SELF_SENTINEL")
    monkeypatch.setattr("row_bot.self_knowledge.build_dynamic_self_knowledge_block", lambda: "")
    monkeypatch.setattr("row_bot.skills.get_skills_prompt", lambda *args, **kwargs: "")
    monkeypatch.setattr("row_bot.plugins.registry.get_skills_prompt", lambda *args, **kwargs: "")
    monkeypatch.setattr("row_bot.memory_policy.build_auto_recall", lambda *args, **kwargs: decision)
    monkeypatch.setattr("row_bot.memory_policy.touch_selected_memories", lambda recall_decision: None)
    monkeypatch.setattr("row_bot.memory_policy.record_recall_trace", lambda recall_decision, **kwargs: None)
    return model_ref


def _agent_request(
    agent,
    model_ref: str,
    *,
    surface: str = "normal_chat",
    config: dict | None = None,
    messages: list | None = None,
) -> list:
    agent._set_active_runtime_context(thread_id="cache-thread", runtime_surface=surface, enabled_tool_names=())
    agent.set_active_model_override(model_ref)
    try:
        return agent._pre_model_trim({
            "execution_budget": new_execution_budget(f"cache-{surface}"),
            "messages": messages or [SystemMessage(content="ROOT_SENTINEL"), HumanMessage(content="hello")],
        }, config)["llm_input_messages"]
    finally:
        agent.set_active_model_override("")


# ── Which route gets which instructions ─────────────────────────────────────


@pytest.mark.parametrize(
    ("provider_id", "model_id", "uses_anthropic_messages", "system_breakpoint", "tail"),
    [
        ("anthropic", "claude-opus-5-5", True, True, True),
        ("openrouter", "anthropic/claude-sonnet-4-5", True, True, True),
        ("requesty", "anthropic/claude-sonnet-4-5", True, False, True),
        ("requesty", "bedrock/claude-sonnet-4-5", True, False, True),
        # Unverified Claude routes: no cache instructions until a live check (B3).
        ("minimax", "MiniMax-M2.7", True, False, False),
        ("opencode_zen", "anthropic/claude-sonnet-4-5", True, False, False),
        ("opencode_go", "anthropic/claude-sonnet-4-5", True, False, False),
        ("claude_subscription", "claude-sonnet-4-5", True, False, False),
        ("atlascloud", "anthropic/claude-sonnet-4-5", False, False, False),
        # Not Claude.
        ("openrouter", "openai/gpt-5.5", False, False, False),
        ("codex", "gpt-5.5", False, False, False),
        ("openai", "gpt-5.5", False, False, False),
        ("custom_openai_lab", "local-chat", False, False, False),
    ],
)
def test_each_claude_route_gets_only_the_cache_instructions_it_documents(
    tmp_path, monkeypatch, provider_id, model_id, uses_anthropic_messages, system_breakpoint, tail, reload_for_data_dir
):
    agent = _fresh_agent(tmp_path, monkeypatch, reload_for_data_dir)
    model_ref = _quiet_agent(agent, monkeypatch, provider_id, model_id)
    monkeypatch.setattr(
        agent,
        "_provider_uses_anthropic_messages",
        lambda pid, _model_id=None: bool(uses_anthropic_messages and pid == provider_id),
    )

    result = _agent_request(agent, model_ref)

    marked = [message for message in result if _has_cache_control(message)]
    if system_breakpoint:
        [marked_message] = marked
        assert isinstance(marked_message, SystemMessage)
        assert _markers([block for block in marked_message.content if isinstance(block, dict)]) == [ONE_HOUR]
    else:
        assert marked == []
    tagged = CONVERSATION_CACHE_KEY in (result[-1].additional_kwargs or {})
    assert tagged is tail
    assert not any(CONVERSATION_CACHE_KEY in (message.additional_kwargs or {}) for message in result[:-1])


def test_openrouter_gets_the_system_prompt_as_blocks_with_the_breakpoint_on_the_last_stable_one(
    tmp_path, monkeypatch, reload_for_data_dir
):
    agent = _fresh_agent(tmp_path, monkeypatch, reload_for_data_dir)
    model_ref = _quiet_agent(agent, monkeypatch, "openrouter", "anthropic/claude-sonnet-4-5")

    result = _agent_request(agent, model_ref, messages=[HumanMessage(content="hello")])

    [system] = [message for message in result if isinstance(message, SystemMessage)]
    assert result[0] is system
    blocks = system.content
    assert isinstance(blocks, list) and len(blocks) > 2
    assert any("SELF_SENTINEL" in block["text"] for block in blocks)
    marked = [index for index, block in enumerate(blocks) if "cache_control" in block]
    # Every system section is stable, so the breakpoint is on the last block.
    assert marked == [len(blocks) - 1]
    assert blocks[-1]["cache_control"] == ONE_HOUR


def test_computer_guide_is_a_tool_belt_bound_stable_section(tmp_path, monkeypatch, reload_for_data_dir):
    agent = _fresh_agent(tmp_path, monkeypatch, reload_for_data_dir)
    import row_bot.skills as skills

    skills.load_skills()
    monkeypatch.setattr(agent, "get_context_size", lambda: 65_536)
    monkeypatch.setattr(agent, "get_current_model", lambda: "model:anthropic:test")
    monkeypatch.setattr(agent, "is_cloud_model", lambda _model: True)
    monkeypatch.setattr(agent, "get_cloud_provider", lambda _model: "anthropic")
    monkeypatch.setattr(
        agent,
        "_provider_uses_anthropic_messages",
        lambda provider_id, _model_id=None: provider_id == "anthropic",
    )
    monkeypatch.setattr(agent, "is_background_workflow", lambda: False)
    monkeypatch.setattr("row_bot.self_knowledge.build_static_self_knowledge_block", lambda: "")
    monkeypatch.setattr("row_bot.self_knowledge.build_dynamic_self_knowledge_block", lambda: "")
    monkeypatch.setattr("row_bot.plugins.registry.get_skills_prompt", lambda *args, **kwargs: "")
    decision = SimpleNamespace(
        allowed=False,
        reason="disabled",
        candidates_seen=0,
        selected=[],
        trace={},
    )
    monkeypatch.setattr("row_bot.memory_policy.build_auto_recall", lambda *args, **kwargs: decision)
    monkeypatch.setattr("row_bot.memory_policy.touch_selected_memories", lambda _decision: None)
    monkeypatch.setattr("row_bot.memory_policy.record_recall_trace", lambda _decision, **kwargs: None)

    fingerprints: list[str] = []
    original_cache_marker = agent.apply_anthropic_system_cache_marker

    def _record_fingerprint(messages, **kwargs):
        fingerprints.append(str(kwargs.get("stable_fingerprint") or ""))
        return original_cache_marker(messages, **kwargs)

    monkeypatch.setattr(agent, "apply_anthropic_system_cache_marker", _record_fingerprint)

    def _assemble(active_tools: tuple[str, ...], turn: str):
        agent._set_active_runtime_context(
            thread_id="computer-guide-cache",
            runtime_surface="normal_chat",
            enabled_tool_names=active_tools,
        )
        agent._current_effective_tool_parent_names_var.set(active_tools)
        result = agent._pre_model_trim(
            {
                "execution_budget": new_execution_budget(turn),
                "messages": [HumanMessage(content="hello")],
            }
        )
        return [
            str(message.content)
            for message in result["llm_input_messages"]
            if isinstance(message, SystemMessage)
        ]

    no_tools = _assemble((), "computer-guide-cache-none")
    no_tools_fingerprint = fingerprints[-1]
    browser_only = _assemble(("browser",), "computer-guide-cache-browser")
    computer = _assemble(("computer_use",), "computer-guide-cache-computer")
    computer_fingerprint = fingerprints[-1]

    assert all("COMPUTER USE WORKFLOW" not in content for content in no_tools)
    assert all("COMPUTER USE WORKFLOW" not in content for content in browser_only)
    assert "COMPUTER USE WORKFLOW" not in computer[0]
    assert sum("COMPUTER USE WORKFLOW" in content for content in computer[1:]) == 1
    assert no_tools_fingerprint != computer_fingerprint


# ── Lifetime follows the conversation (D3) ──────────────────────────────────


@pytest.mark.parametrize(
    ("surface", "background", "config", "expected"),
    [
        ("normal_chat", False, None, ONE_HOUR),
        ("remote_client", False, None, ONE_HOUR),
        ("channel", False, None, ONE_HOUR),
        ("approval", False, None, ONE_HOUR),
        ("designer", False, None, ONE_HOUR),
        ("workflow", True, None, FIVE_MINUTES),
        ("goal", False, None, FIVE_MINUTES),
        ("normal_chat", False, {"configurable": {"internal_goal_continuation": True}}, FIVE_MINUTES),
        ("agent_child", False, None, FIVE_MINUTES),
        ("normal_chat", True, None, FIVE_MINUTES),
    ],
)
def test_the_cache_lifetime_follows_the_conversation(tmp_path, monkeypatch, surface, background, config, expected, reload_for_data_dir):
    agent = _fresh_agent(tmp_path, monkeypatch, reload_for_data_dir)
    model_ref = _quiet_agent(agent, monkeypatch, "anthropic", "claude-opus-5-5")
    monkeypatch.setattr(agent, "is_background_workflow", lambda: background)

    messages = _agent_request(agent, model_ref, surface=surface, config=config)
    body = CancellableChatAnthropic(model="claude-opus-5-5", api_key="test-key", prompt_cache=True)._get_request_payload(
        messages
    )

    # The system breakpoint and the tail share the lifetime, so a longer one never follows a shorter one.
    assert body["cache_control"] == expected
    assert [block["cache_control"] for block in body["system"] if "cache_control" in block] == [expected]
    assert _message_markers(body) == []
    assert len(_markers(body)) <= cache_breakpoints.MAX_BREAKPOINTS


# ── The direct API request ──────────────────────────────────────────────────


def _direct(**kwargs: Any) -> CancellableChatAnthropic:
    return CancellableChatAnthropic(
        model="claude-opus-5-5",
        api_key="test-key",
        base_url="https://api.anthropic.test",
        max_retries=0,
        prompt_cache=True,
        **kwargs,
    )


def _system(ttl: str = "1h") -> SystemMessage:
    message = SystemMessage(content="STABLE SYSTEM PROMPT")
    marked, _ = apply_anthropic_system_cache_marker(
        [message],
        provider_id="anthropic",
        model_id="claude-opus-5-5",
        cache_eligible_system_message_ids={id(message)},
        ttl=ttl,
    )
    return marked[0]


def _tool_round(index: int) -> list:
    call = {"id": f"call_{index}", "name": "read_file", "args": {"path": f"f{index}.txt"}, "type": "tool_call"}
    return [
        AIMessage(content=[{"type": "text", "text": f"Reading file {index}."}], tool_calls=[call]),
        ToolMessage(content=f"contents of file {index}", tool_call_id=f"call_{index}"),
    ]


def test_the_conversation_tail_is_cached_with_top_level_automatic_caching():
    body = _direct()._get_request_payload(
        with_conversation_cache([_system(), HumanMessage(content="hello"), *_tool_round(1)], ttl="1h")
    )

    assert body["cache_control"] == ONE_HOUR
    assert body["system"][-1]["cache_control"] == ONE_HOUR
    # The tail itself carries no explicit marker: automatic caching places it on the last block.
    assert _message_markers(body) == []
    assert len(_markers(body)) == 2


def test_a_tail_never_outlives_the_system_breakpoint():
    body = _direct()._get_request_payload(
        with_conversation_cache([_system("5m"), HumanMessage(content="hello")], ttl="1h")
    )

    assert body["system"][-1]["cache_control"] == FIVE_MINUTES
    assert body["cache_control"] == FIVE_MINUTES


def test_a_long_append_gets_one_breakpoint_where_the_previous_request_ended():
    model = _direct()
    history = [_system(), HumanMessage(content="Summarise every file.")]
    first = model._get_request_payload(with_conversation_cache(history, ttl="1h"))
    assert _message_markers(first) == []

    # One tool round adds three positions: no intermediate breakpoint.
    history += _tool_round(1)
    second = model._get_request_payload(with_conversation_cache(history, ttl="1h"))
    assert _message_markers(second) == []

    # Eight more rounds (24 positions) would push the previous entry out of the 20-position lookback.
    previous_tail = len(second["messages"]) - 1
    for index in range(2, 10):
        history += _tool_round(index)
    third = model._get_request_payload(with_conversation_cache(history, ttl="1h"))

    [(message_index, block_index)] = _message_markers(third)
    assert message_index == previous_tail
    assert third["messages"][message_index]["content"][block_index]["type"] == "tool_result"
    assert third["messages"][message_index]["content"][block_index]["cache_control"] == ONE_HOUR
    assert third["cache_control"] == ONE_HOUR
    assert len(_markers(third)) == 3 <= cache_breakpoints.MAX_BREAKPOINTS
    last = third["messages"][-1]["content"]
    assert not any("cache_control" in block for block in last if isinstance(block, dict))


def test_a_person_message_where_the_previous_request_ended_stays_as_it_is_when_a_later_block_can_be_marked():
    model = _direct()
    history = [_system(), HumanMessage(content="Summarise every file.")]
    model._get_request_payload(with_conversation_cache(history, ttl="1h"))
    for index in range(1, 9):
        history += _tool_round(index)

    body = model._get_request_payload(with_conversation_cache(history, ttl="1h"))

    assert body["messages"][0]["content"] == "Summarise every file."
    [(message_index, block_index)] = _message_markers(body)
    assert (message_index, body["messages"][message_index]["content"][block_index]["type"]) == (1, "text")


def test_requests_that_are_not_conversations_get_no_tail_breakpoint():
    body = _direct()._get_request_payload([_system("5m"), HumanMessage(content="Name this conversation.")])

    assert "cache_control" not in body
    assert _message_markers(body) == []
    assert body["system"][-1]["cache_control"] == FIVE_MINUTES


def test_stray_history_markers_are_dropped_so_a_request_never_exceeds_four_breakpoints():
    marked_history = [
        HumanMessage(content=[{"type": "text", "text": f"note {index}", "cache_control": ONE_HOUR}])
        for index in range(5)
    ]
    body = _direct()._get_request_payload(with_conversation_cache([_system(), *marked_history], ttl="1h"))

    assert len(_markers(body)) == 2


def test_anthropic_format_gateways_never_get_cache_instructions(monkeypatch):
    import row_bot.providers.runtime as runtime

    monkeypatch.setattr(runtime, "get_provider_secret", lambda *_args, **_kwargs: "test-key")
    direct = runtime.create_chat_model("claude-opus-5-5", provider_id="anthropic")
    gateways = [
        runtime.create_chat_model("claude-opus-5-5", provider_id="minimax"),
        runtime.create_chat_model("model:opencode_zen:claude-opus-5-5"),
    ]

    assert direct.prompt_cache is True
    for gateway in gateways:
        assert gateway.prompt_cache is False
        body = gateway._get_request_payload(with_conversation_cache([HumanMessage(content="hello")], ttl="1h"))
        assert _markers(body) == []


# ── Safety net on the direct API ────────────────────────────────────────────


class _FakeMessagesAPI:
    """Answers Anthropic Messages requests in order and records each request body."""

    def __init__(self, *answers: tuple[int, Any]) -> None:
        self.answers = list(answers)
        self.requests: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append(body)
        status, answer = self.answers.pop(0)
        if status != 200:
            error = {"type": "error", "error": {"type": "invalid_request_error", "message": answer}}
            return httpx.Response(status, json=error)
        usage = {"input_tokens": 3, "output_tokens": 0, **(answer or {})}
        message = {
            "id": f"msg_{len(self.requests)}",
            "type": "message",
            "role": "assistant",
            "model": body["model"],
            "content": [{"type": "text", "text": "done"}],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {**usage, "output_tokens": 1},
        }
        if not body.get("stream"):
            return httpx.Response(200, json=message)
        start = {**message, "content": [], "stop_reason": None, "usage": usage}
        events = [
            {"type": "message_start", "message": start},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "done"}},
            {"type": "content_block_stop", "index": 0},
            # The final usage is cumulative and repeats the input and cache counts.
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                "usage": {**usage, "output_tokens": 1},
            },
            {"type": "message_stop"},
        ]
        content = "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=content.encode())


@pytest.fixture
def fake_api(monkeypatch):
    """Route the production SDK clients of ``CancellableChatAnthropic`` to a fake Messages API."""
    holder: dict[str, _FakeMessagesAPI] = {}
    transport = httpx.MockTransport(lambda request: holder["api"](request))
    sync_client = anthropic_cancellable.cancellable_http_client
    async_client = anthropic_cancellable.cancellable_async_http_client
    monkeypatch.setattr(
        anthropic_cancellable,
        "cancellable_http_client",
        lambda **kwargs: sync_client(transport=transport, **kwargs),
    )
    monkeypatch.setattr(
        anthropic_cancellable,
        "cancellable_async_http_client",
        lambda **kwargs: async_client(transport=transport, **kwargs),
    )

    def install(*answers: tuple[int, Any]) -> _FakeMessagesAPI:
        holder["api"] = _FakeMessagesAPI(*answers)
        return holder["api"]

    return install


@pytest.mark.parametrize("mode", ["invoke", "stream"])
def test_a_rejected_cache_instruction_is_retried_once_without_any(fake_api, mode):
    api = fake_api(
        (400, "cache_control: a ttl='1h' entry cannot come after a ttl='5m' entry"),
        (200, None),
    )
    request = with_conversation_cache([_system(), HumanMessage(content="hello")], ttl="1h")
    model = _direct()

    result = model.invoke(request) if mode == "invoke" else list(model.stream(request))

    assert result
    first, retry = api.requests
    assert _markers(first) and _markers(retry) == []
    assert retry["messages"] == first["messages"]
    # The direct API documents every instruction, so the route is not switched off: the next
    # conversation request carries them again.
    again = fake_api((200, None))
    model.invoke(request)
    assert _markers(again.requests[0])


@pytest.mark.parametrize(
    "message",
    [
        "prompt is too long: 1000001 tokens > 1000000 maximum",
        "messages.1.content.0: tool_use ids must be unique",
    ],
)
def test_other_bad_requests_are_not_retried(fake_api, message):
    api = fake_api((400, message), (200, None))

    with pytest.raises(Exception):
        _direct().invoke(with_conversation_cache([_system(), HumanMessage(content="hello")], ttl="1h"))

    assert len(api.requests) == 1


# ── OpenRouter ──────────────────────────────────────────────────────────────


@pytest.fixture
def openrouter_api(monkeypatch):
    """A fake OpenRouter chat API behind the production SDK client."""
    state: dict[str, Any] = {"answers": [], "requests": []}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        state["requests"].append(body)
        status, message = state["answers"].pop(0) if state["answers"] else (200, None)
        if status != 200:
            return httpx.Response(status, json={"error": {"code": status, "message": message}})
        return httpx.Response(200, json={
            "id": "gen-1",
            "object": "chat.completion",
            "created": 1,
            "system_fingerprint": None,
            "model": body["model"],
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "done"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
        })

    transport = httpx.MockTransport(handler)
    sync_client = openrouter_cancellable.cancellable_http_client
    monkeypatch.setattr(
        openrouter_cancellable,
        "cancellable_http_client",
        lambda **kwargs: sync_client(transport=transport, **kwargs),
    )
    return state


def _openrouter(model_name: str = "anthropic/claude-sonnet-4.5") -> CancellableChatOpenRouter:
    return CancellableChatOpenRouter(model_name=model_name, openrouter_api_key="test-key", max_retries=0)


def _openrouter_request(model_id: str = "anthropic/claude-sonnet-4.5", *, tail: bool = True) -> list:
    root, rules = SystemMessage(content="STABLE SYSTEM PROMPT"), SystemMessage(content="MORE STABLE RULES")
    messages, _ = apply_anthropic_system_cache_marker(
        [root, rules, HumanMessage(content="hello")],
        provider_id="openrouter",
        model_id=model_id,
        cache_eligible_system_message_ids={id(root), id(rules)},
        ttl="1h",
    )
    return with_conversation_cache(messages, ttl="1h") if tail else messages


def test_openrouter_claude_requests_carry_its_documented_cache_fields(openrouter_api):
    _openrouter().invoke(_openrouter_request())
    _openrouter().invoke(_openrouter_request(tail=False))
    _openrouter("openai/gpt-5.5").invoke(with_conversation_cache([HumanMessage(content="hello")], ttl="1h"))

    conversation, single_call, other_model = openrouter_api["requests"]
    system = conversation["messages"][0]
    assert system["role"] == "system"
    assert [block.get("cache_control") for block in system["content"]] == [None, ONE_HOUR]
    assert [block["text"] for block in system["content"]] == ["STABLE SYSTEM PROMPT", "MORE STABLE RULES"]
    assert conversation["cache_control"] == ONE_HOUR
    assert single_call.get("cache_control") is None
    assert _markers(other_model) == []


def test_openrouter_rejection_is_retried_once_and_remembered(openrouter_api):
    openrouter_api["answers"] = [(400, "cache_control.ttl is not supported for this provider")]

    assert _openrouter().invoke(_openrouter_request()).content == "done"
    _openrouter().invoke(_openrouter_request())

    first, retry, later = openrouter_api["requests"]
    assert _markers(first)
    assert _markers(retry) == [] and _markers(later) == []


def test_openrouter_other_bad_requests_are_not_retried(openrouter_api):
    openrouter_api["answers"] = [(400, "This model's maximum context length is 200000 tokens")]

    with pytest.raises(Exception):
        _openrouter().invoke(_openrouter_request())

    assert len(openrouter_api["requests"]) == 1


# ── Requesty ────────────────────────────────────────────────────────────────


def _requesty(model_name: str = "anthropic/claude-sonnet-4-5", handler=None, *, profile: str = "requesty"):
    client = httpx.Client(transport=httpx.MockTransport(handler)) if handler else None
    return ChatOpenAICompatible(
        model_name=model_name,
        api_key="test-key",
        base_url="https://router.requesty.test/v1",
        endpoint={"provider_id": profile, "display_name": profile.title(), "transport": "openai_chat",
                  "profile": profile, "base_url": "https://router.requesty.test/v1"},
        http_client=client,
    )


def test_requesty_claude_conversations_ask_for_auto_cache():
    tagged = with_conversation_cache([SystemMessage(content="SYSTEM"), HumanMessage(content="hello")], ttl="1h")

    body = _requesty()._request_body(tagged, stream=True)

    assert body["requesty"] == {"auto_cache": True}
    # Requesty places its own breakpoints; Row-Bot sends no block markers there.
    assert _markers(body) == []
    assert "requesty" not in _requesty()._request_body(tagged[:-1] + [HumanMessage(content="x")], stream=False)
    assert "requesty" not in _requesty("openai/gpt-5.5")._request_body(tagged, stream=False)
    assert "requesty" not in _requesty(profile="atlascloud")._request_body(tagged, stream=False)


def test_requesty_rejection_is_retried_once_and_remembered():
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return httpx.Response(400, json={"error": {"message": "unknown field requesty.auto_cache"}})
        return httpx.Response(200, json={
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "done"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
        })

    model = _requesty(handler=handler)
    tagged = with_conversation_cache([HumanMessage(content="hello")], ttl="1h")

    assert model.invoke(tagged).content == "done"
    assert model.invoke(tagged).content == "done"

    first, retry, later = requests
    assert first["requesty"] == {"auto_cache": True}
    assert "requesty" not in retry and "requesty" not in later


# ── The usage ledger sees the cache ─────────────────────────────────────────


def test_the_ledger_records_cache_writes_then_reads_for_consecutive_calls(fake_api, monkeypatch, tmp_path):
    import row_bot.providers.runtime as runtime

    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(runtime, "get_provider_secret", lambda *_args, **_kwargs: "test-key")
    first_write = {
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 30_000,
        "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 30_000},
    }
    warm_read = {
        "cache_read_input_tokens": 30_000,
        "cache_creation_input_tokens": 800,
        "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 800},
    }
    api = fake_api((200, first_write), (200, warm_read))
    model = runtime.create_chat_model("claude-opus-5-5", provider_id="anthropic")
    history = [_system(), HumanMessage(content="hello")]

    list(model.stream(with_conversation_cache(history, ttl="1h")))
    history += _tool_round(1)
    list(model.stream(with_conversation_cache(history, ttl="1h")))

    assert [request["cache_control"] for request in api.requests] == [ONE_HOUR, ONE_HOUR]
    with closing(sqlite3.connect(tmp_path / "usage.db")) as connection:
        rows = connection.execute(
            "SELECT input_tokens, cache_read_tokens, cache_write_tokens, cache_write_1h_tokens"
            " FROM model_usage ORDER BY id"
        ).fetchall()
    # The healthy loop: the second call reads the whole earlier prefix and writes only its new part.
    assert rows == [(30_003, 0, 30_000, 30_000), (30_803, 30_000, 800, 800)]
