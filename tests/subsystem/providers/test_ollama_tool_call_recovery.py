"""A local Ollama model gets one more try when Ollama can't hand back its tool call.

Two failures ended local-model turns (seen with Qwen3.5 and qwen3:8b through Row-Bot's real runtime):

- the model writes a slightly malformed tool call and Ollama's parser answers HTTP 500 ("XML syntax
  error …") before anything streams, so the whole turn ended as "The AI provider had a server error";
- the model calls a tool that isn't bound in the request (one of the deferred tools behind
  ``tool_search``/``tool_invoke``), Ollama drops the call, and the reply comes back empty.

Each case is asked again once, with a short note saying what to do. These tests drive the local chat
model against a fake Ollama on loopback and read what reached it.
"""

from __future__ import annotations

import asyncio

import pytest
from langchain_core.messages import HumanMessage
from langchain_core.tools import StructuredTool

from row_bot.providers.transports.ollama_local import RowBotChatOllama
from tests.helpers.fake_ollama import fake_streaming_ollama

MODEL = "fake-local:7b"
QUESTION = "What is 2 + 2?"
PARSE_ERROR = "XML syntax error on line 8: element <function> closed by </parameter>"


def _calculate(expression: str) -> str:
    return "4"


def _tool_invoke(name: str, arguments: dict) -> str:
    return "ok"


CALCULATE = StructuredTool.from_function(_calculate, name="calculate", description="Calculate.")
TOOL_INVOKE = StructuredTool.from_function(_tool_invoke, name="tool_invoke", description="Run a tool by name.")
CALCULATE_CALL = {"function": {"name": "calculate", "arguments": {"expression": "2 + 2"}}}
INVOKE_CALL = {"function": {"name": "tool_invoke",
                            "arguments": {"name": "calendar_update_event", "arguments": {"id": "evt_1"}}}}


def _ask(server, tools: list | None, *, use_async: bool = False):
    chat = RowBotChatOllama(model=MODEL, base_url=server.url)
    model = chat.bind_tools(tools) if tools else chat
    prompt = [HumanMessage(content=QUESTION)]
    return asyncio.run(model.ainvoke(prompt)) if use_async else model.invoke(prompt)


@pytest.mark.parametrize("use_async", [False, True], ids=["invoke", "ainvoke"])
def test_a_malformed_tool_call_is_asked_again_once_with_a_note(use_async: bool) -> None:
    replies = [{"status": 500, "error": PARSE_ERROR}, {"tool_calls": [CALCULATE_CALL]}]
    with fake_streaming_ollama(tokens=[], model=MODEL, replies=replies) as server:
        reply = _ask(server, [CALCULATE], use_async=use_async)
        first, second = server.chat_requests()

    assert [call["name"] for call in reply.tool_calls] == ["calculate"]
    assert first["messages"][-1]["content"] == QUESTION
    # The second request is the first plus one note from Row-Bot about how to write the call.
    assert second["messages"][:-1] == first["messages"]
    note = second["messages"][-1]
    assert note["role"] == "user"
    assert "<parameter=" in note["content"]


def test_a_malformed_tool_call_twice_still_ends_the_call() -> None:
    with fake_streaming_ollama(tokens=[], model=MODEL, replies=[{"status": 500, "error": PARSE_ERROR}]) as server:
        with pytest.raises(Exception, match="XML syntax error"):
            _ask(server, [CALCULATE])
        assert len(server.chat_requests()) == 2


def test_any_other_server_error_is_not_asked_again() -> None:
    replies = [{"status": 500, "error": "model runner has unexpectedly stopped"}, {"tool_calls": [CALCULATE_CALL]}]
    with fake_streaming_ollama(tokens=[], model=MODEL, replies=replies) as server:
        with pytest.raises(Exception, match="unexpectedly stopped"):
            _ask(server, [CALCULATE])
        assert len(server.chat_requests()) == 1


@pytest.mark.parametrize("use_async", [False, True], ids=["invoke", "ainvoke"])
def test_an_empty_reply_with_tools_is_asked_again_pointing_to_tool_invoke(use_async: bool) -> None:
    replies = [
        {"eval_count": 57, "prompt_eval_count": 900},  # the model called a tool Ollama dropped
        {"tool_calls": [INVOKE_CALL], "eval_count": 20, "prompt_eval_count": 950},
    ]
    with fake_streaming_ollama(tokens=[], model=MODEL, replies=replies) as server:
        reply = _ask(server, [CALCULATE, TOOL_INVOKE], use_async=use_async)
        first, second = server.chat_requests()

    assert [call["name"] for call in reply.tool_calls] == ["tool_invoke"]
    assert reply.tool_calls[0]["args"]["name"] == "calendar_update_event"
    assert second["messages"][:-1] == first["messages"]
    note = second["messages"][-1]
    assert note["role"] == "user" and "tool_invoke" in note["content"]
    # Both attempts' tokens are counted.
    assert reply.usage_metadata["input_tokens"] == 1850
    assert reply.usage_metadata["output_tokens"] == 77


def test_without_tool_invoke_the_note_asks_to_answer_or_use_a_listed_tool() -> None:
    replies = [{"eval_count": 30}, {"tokens": ["4"], "eval_count": 2}]
    with fake_streaming_ollama(tokens=[], model=MODEL, replies=replies) as server:
        reply = _ask(server, [CALCULATE])
        note = server.chat_requests()[1]["messages"][-1]

    assert reply.content == "4"
    assert "tool_invoke" not in note["content"]
    assert note["role"] == "user"


def test_an_empty_reply_twice_comes_back_empty() -> None:
    with fake_streaming_ollama(tokens=[], model=MODEL, replies=[{"eval_count": 30}]) as server:
        reply = _ask(server, [CALCULATE, TOOL_INVOKE])
        assert len(server.chat_requests()) == 2

    assert reply.content == ""
    assert reply.tool_calls == []


def test_an_empty_reply_in_plain_chat_is_not_asked_again() -> None:
    with fake_streaming_ollama(tokens=[], model=MODEL, replies=[{"eval_count": 30}]) as server:
        _ask(server, None)
        assert len(server.chat_requests()) == 1


def test_an_answer_or_a_tool_call_goes_through_untouched() -> None:
    replies = [{"tokens": ["Four", "."], "eval_count": 3}]
    with fake_streaming_ollama(tokens=[], model=MODEL, replies=replies) as server:
        reply = _ask(server, [CALCULATE])
        assert len(server.chat_requests()) == 1
    assert reply.content == "Four."

    with fake_streaming_ollama(tokens=[], model=MODEL, replies=[{"tool_calls": [CALCULATE_CALL]}]) as server:
        reply = _ask(server, [CALCULATE])
        assert len(server.chat_requests()) == 1
    assert [call["name"] for call in reply.tool_calls] == ["calculate"]
