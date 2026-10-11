"""Encrypted reasoning is kept, replayed unchanged to its own provider, and never sent anywhere else (WP B2, F11).

Requests are stateless (``store: false``), so a reasoning model keeps its
reasoning across a turn's calls only when the client sends the reasoning items
back. HTTP is answered in process; the tests read what would reach the wire.
"""

from __future__ import annotations

import json
import operator
from functools import reduce
from types import SimpleNamespace
from typing import Any, Callable

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

import row_bot.providers.runtime as runtime
from row_bot.providers.transports.codex_responses import ChatCodexResponses
from row_bot.providers.transports.reasoning_fallback import should_retry_reasoning_error
from row_bot.providers.transports.responses_reasoning import REASONING_REPLAY_KEY, reasoning_replay_kwargs
from row_bot.providers.transports.xai_oauth_responses import ChatXAIOAuthResponses

CIPHERTEXT = "gAAAA-final-ciphertext"
TOOL = {"type": "function", "function": {
    "name": "lookup", "description": "Look something up.",
    "parameters": {"type": "object", "properties": {"q": {"type": "string"}}},
}}
REJECTION = "The encrypted content for item rs_1 could not be verified."


def _sse(*events: dict) -> list[bytes]:
    lines: list[bytes] = []
    for event in events:
        lines += [f"event: {event['type']}".encode(), f"data: {json.dumps(event)}".encode(), b""]
    return lines


REASONING_ITEM = {
    "type": "reasoning", "id": "rs_1", "status": "completed",
    "summary": [{"type": "summary_text", "text": "Look it up first."}],
    "encrypted_content": CIPHERTEXT,
}
TOOL_TURN = _sse(
    {"type": "response.output_item.done", "output_index": 0, "item": REASONING_ITEM},
    {"type": "response.output_item.done", "output_index": 1, "item": {
        "type": "function_call", "id": "fc_1", "call_id": "call_1", "name": "lookup", "arguments": '{"q":"x"}',
    }},
    {"type": "response.completed", "response": {"id": "resp_1"}},
)
TEXT_TURN = _sse(
    {"type": "response.output_text.delta", "delta": "Done."},
    {"type": "response.completed", "response": {"id": "resp_2"}},
)


class _Client:
    """Answers Responses requests in order: SSE lines, or (status, error text)."""

    def __init__(self, *answers: Any) -> None:
        self.answers = list(answers)
        self.calls: list[dict] = []

    def post(self, url: str, **kwargs: Any) -> Any:
        self.calls.append(kwargs["json"])
        answer = self.answers.pop(0)
        if isinstance(answer, tuple):
            status, message = answer
            payload = {"error": {"message": message}}
            return SimpleNamespace(status_code=status, text=json.dumps(payload), json=lambda: payload,
                                   iter_lines=lambda: iter(()))
        return SimpleNamespace(status_code=200, text="", iter_lines=lambda: iter(answer))

    def close(self) -> None:
        pass


@pytest.fixture(autouse=True)
def subscription_credentials(monkeypatch) -> None:
    from row_bot.providers import codex, xai_oauth

    credentials = SimpleNamespace(access_token="access", account_id="account", refresh_token="")
    monkeypatch.setattr(codex, "codex_runtime_credentials", lambda **kwargs: credentials)
    monkeypatch.setattr(xai_oauth, "xai_oauth_runtime_credentials", lambda **kwargs: credentials)


TRANSPORTS: dict[str, Callable[..., Any]] = {
    "codex": lambda client, model="gpt-5.5": ChatCodexResponses(model_name=model, http_client=client),
    "xai_oauth": lambda client, model="grok-4.6": ChatXAIOAuthResponses(
        model_name=model, base_url="https://api.x.ai/v1", http_client=client,
    ),
}
MODELS = {"codex": "gpt-5.5", "xai_oauth": "grok-4.6"}
REPLAYED_ITEM = {key: value for key, value in REASONING_ITEM.items() if key != "status"}


def _tool_turn(transport: str, *, stream: bool) -> AIMessage:
    model = TRANSPORTS[transport](_Client(TOOL_TURN)).bind_tools([TOOL])
    if stream:
        return reduce(operator.add, model.stream([HumanMessage(content="look x up")]))
    return model.invoke([HumanMessage(content="look x up")])


def _follow_up(ai: AIMessage) -> list:
    return [HumanMessage(content="look x up"), ai, ToolMessage(content="x is 42", tool_call_id="call_1")]


@pytest.mark.parametrize("transport", sorted(TRANSPORTS))
@pytest.mark.parametrize("stream", [True, False], ids=["stream", "invoke"])
def test_the_response_keeps_its_reasoning_items_for_replay(transport, stream) -> None:
    ai = _tool_turn(transport, stream=stream)

    assert ai.tool_calls[0]["id"] == "call_1"
    assert ai.additional_kwargs[REASONING_REPLAY_KEY] == {
        "transport": transport, "model": MODELS[transport], "items": [REPLAYED_ITEM],
    }


@pytest.mark.parametrize("transport", sorted(TRANSPORTS))
def test_reasoning_is_replayed_unchanged_before_its_tool_call(transport) -> None:
    ai = _tool_turn(transport, stream=True)
    client = _Client(TEXT_TURN)

    TRANSPORTS[transport](client).bind_tools([TOOL]).invoke(_follow_up(ai))

    body = client.calls[0]
    assert body["store"] is False
    assert body["include"] == ["reasoning.encrypted_content"]
    assert [item.get("type") for item in body["input"]] == [
        "message", "reasoning", "function_call", "function_call_output",
    ]
    assert body["input"][1] == REPLAYED_ITEM


@pytest.mark.parametrize("transport", sorted(TRANSPORTS))
def test_reasoning_from_another_model_is_not_replayed(transport) -> None:
    ai = _tool_turn(transport, stream=True)
    client = _Client(TEXT_TURN)

    other_model = "gpt-5.4" if transport == "codex" else "grok-4.5"
    TRANSPORTS[transport](client, other_model).invoke(_follow_up(ai))

    assert CIPHERTEXT not in json.dumps(client.calls[0])


def test_reasoning_survives_the_agent_graph_and_its_checkpoint() -> None:
    from langchain_core.tools import tool
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.prebuilt import create_react_agent

    @tool
    def lookup(q: str) -> str:
        """Look something up."""
        return "x is 42"

    client = _Client(TOOL_TURN, TEXT_TURN, TEXT_TURN)
    graph = create_react_agent(
        model=ChatCodexResponses(model_name="gpt-5.5", http_client=client), tools=[lookup], checkpointer=InMemorySaver(),
    )
    config = {"configurable": {"thread_id": "replay-thread"}}

    for text in ("look x up", "and again?"):
        list(graph.stream({"messages": [HumanMessage(content=text)]}, config, stream_mode="messages"))

    assert [[item.get("type") for item in call["input"]] for call in client.calls] == [
        ["message"],
        ["message", "reasoning", "function_call", "function_call_output"],  # the same turn's next call
        ["message", "reasoning", "function_call", "function_call_output", "message", "message"],  # the next turn
    ]
    assert client.calls[2]["input"][1] == REPLAYED_ITEM


def test_reasoning_from_another_transport_is_not_replayed() -> None:
    codex_ai = _tool_turn("codex", stream=True)
    client = _Client(TEXT_TURN)

    TRANSPORTS["xai_oauth"](client, "gpt-5.5").invoke(_follow_up(codex_ai))

    assert CIPHERTEXT not in json.dumps(client.calls[0])


@pytest.mark.parametrize("transport", sorted(TRANSPORTS))
def test_unreadable_reasoning_is_retried_once_without_it(transport, caplog) -> None:
    ai = _tool_turn(transport, stream=True)
    client = _Client((400, REJECTION), TEXT_TURN)

    result = TRANSPORTS[transport](client).bind_tools([TOOL]).invoke(_follow_up(ai))

    assert result.content == "Done."
    assert len(client.calls) == 2
    assert CIPHERTEXT in json.dumps(client.calls[0])
    assert [item.get("type") for item in client.calls[1]["input"]] == [
        "message", "function_call", "function_call_output",
    ]
    assert "replayed reasoning was rejected" in caplog.text
    assert CIPHERTEXT not in caplog.text


@pytest.mark.parametrize("transport", sorted(TRANSPORTS))
def test_other_rejections_are_not_retried(transport) -> None:
    ai = _tool_turn(transport, stream=True)
    client = _Client((400, "Invalid value for tool_choice."))

    with pytest.raises(RuntimeError, match="HTTP 400"):
        TRANSPORTS[transport](client).bind_tools([TOOL]).invoke(_follow_up(ai))

    assert len(client.calls) == 1


def test_an_unreadable_replay_is_not_a_reasoning_effort_fallback() -> None:
    rejected = RuntimeError(f"Codex Responses request failed with HTTP 400: {REJECTION}")
    effort = RuntimeError("Codex Responses request failed with HTTP 400: Unsupported reasoning effort 'xhigh'.")

    assert should_retry_reasoning_error(rejected) is False
    assert should_retry_reasoning_error(effort) is True


# --- Direct OpenAI (langchain-openai, Responses, store=False) -----------------------------------


def _openai_event(event_type: str, **fields: Any) -> dict:
    return {"type": event_type, "sequence_number": 0, **fields}


def _openai_stream(partial: str, final: str) -> str:
    response = {"id": "resp_1", "object": "response", "created_at": 0, "model": "gpt-5.4",
                "parallel_tool_calls": True, "tool_choice": "auto", "tools": []}
    function_call = {"type": "function_call", "id": "fc_1", "call_id": "call_1", "name": "lookup",
                     "arguments": '{"q":"x"}', "status": "completed"}
    final_reasoning = {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": final}
    events = [
        _openai_event("response.created", response={**response, "status": "in_progress", "output": []}),
        # OpenAI: the ciphertext on an in-progress item may be incomplete.
        _openai_event("response.output_item.added", output_index=0,
                      item={"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": partial}),
        _openai_event("response.output_item.done", output_index=0, item=final_reasoning),
        _openai_event("response.output_item.added", output_index=1, item={**function_call, "arguments": ""}),
        _openai_event("response.function_call_arguments.delta", output_index=1, item_id="fc_1", delta='{"q":"x"}'),
        _openai_event("response.output_item.done", output_index=1, item=function_call),
        _openai_event("response.completed", response={
            **response, "status": "completed", "output": [final_reasoning, function_call],
            "usage": {"input_tokens": 5, "output_tokens": 3, "total_tokens": 8,
                      "input_tokens_details": {"cached_tokens": 0}, "output_tokens_details": {"reasoning_tokens": 2}},
        }),
    ]
    return "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events)


def _openai_text_stream() -> str:
    message = {"type": "message", "id": "msg_2", "role": "assistant", "status": "completed",
               "content": [{"type": "output_text", "text": "Done.", "annotations": []}]}
    response = {"id": "resp_2", "object": "response", "created_at": 0, "model": "gpt-5.4",
                "parallel_tool_calls": True, "tool_choice": "auto", "tools": []}
    events = [
        _openai_event("response.created", response={**response, "status": "in_progress", "output": []}),
        _openai_event("response.output_item.added", output_index=0, item={**message, "content": []}),
        _openai_event("response.output_text.delta", output_index=0, content_index=0, item_id="msg_2", delta="Done."),
        _openai_event("response.completed", response={**response, "status": "completed", "output": [message]}),
    ]
    return "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events)


_OPENAI_TEXT_RESPONSE = {
    "id": "resp_2", "object": "response", "created_at": 0, "model": "gpt-5.4", "status": "completed",
    "output": [{"type": "message", "id": "msg_2", "role": "assistant", "status": "completed",
                "content": [{"type": "output_text", "text": "Done.", "annotations": []}]}],
    "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
    "parallel_tool_calls": True, "tool_choice": "auto", "tools": [],
}


@pytest.fixture
def openai_api(monkeypatch) -> SimpleNamespace:
    """The OpenAI Responses API in process: queued answers, recorded request bodies."""
    api = SimpleNamespace(bodies=[], answers=[])

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        api.bodies.append(json.loads(request.content))
        answer = api.answers.pop(0)
        if isinstance(answer, str):
            return httpx.Response(200, text=answer, headers={"content-type": "text/event-stream"}, request=request)
        if isinstance(answer, tuple):
            status, message = answer
            return httpx.Response(status, json={"error": {"message": message, "type": "invalid_request_error"}},
                                  request=request)
        return httpx.Response(200, json=answer, request=request)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", handle_request)
    monkeypatch.setattr(runtime, "get_provider_secret", lambda provider_id, *args, **kwargs: "test-key")
    return api


def _openai_model() -> Any:
    return runtime.create_chat_model("gpt-5.4", provider_id="openai").bind_tools([TOOL])


def test_openai_streamed_reasoning_keeps_its_final_ciphertext_and_is_replayed(openai_api) -> None:
    openai_api.answers = [_openai_stream(partial="gAAAA-fin", final=CIPHERTEXT), _OPENAI_TEXT_RESPONSE]
    model = _openai_model()

    ai = reduce(operator.add, model.stream([HumanMessage(content="look x up")]))
    model.invoke(_follow_up(ai))

    reasoning = [block for block in ai.content if isinstance(block, dict) and block.get("type") == "reasoning"]
    assert [block["encrypted_content"] for block in reasoning] == [CIPHERTEXT]
    first, follow_up = openai_api.bodies
    assert first["store"] is False and first["include"] == ["reasoning.encrypted_content"]
    replayed = follow_up["input"]
    assert [item.get("type") for item in replayed] == ["message", "reasoning", "function_call", "function_call_output"]
    assert replayed[1]["encrypted_content"] == CIPHERTEXT
    assert replayed[2]["id"] == "fc_1"


def test_openai_async_stream_keeps_the_final_ciphertext(monkeypatch) -> None:
    import asyncio

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_openai_stream(partial="gAAAA-fin", final=CIPHERTEXT),
                              headers={"content-type": "text/event-stream"}, request=request)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", handle_async_request)
    monkeypatch.setattr(runtime, "get_provider_secret", lambda provider_id, *args, **kwargs: "test-key")
    model = _openai_model()

    async def collect() -> AIMessage:
        return reduce(operator.add, [chunk async for chunk in model.astream([HumanMessage(content="look x up")])])

    ai = asyncio.run(collect())

    assert [block["encrypted_content"] for block in ai.content if block.get("type") == "reasoning"] == [CIPHERTEXT]


def test_openai_reasoning_kept_server_side_earlier_is_not_referenced(openai_api) -> None:
    """A conversation from before store=False holds reasoning without its ciphertext."""
    openai_api.answers = [_OPENAI_TEXT_RESPONSE]
    stored_turn = AIMessage(
        content=[
            {"type": "reasoning", "id": "rs_old", "summary": []},
            {"type": "function_call", "id": "fc_old", "call_id": "call_1", "name": "lookup", "arguments": "{}"},
        ],
        tool_calls=[{"name": "lookup", "args": {}, "id": "call_1", "type": "tool_call"}],
        response_metadata={"model_provider": "openai"},
    )
    answered = AIMessage(content=[{"type": "text", "text": "x is 42", "id": "msg_old"}],
                         response_metadata={"model_provider": "openai"})

    _openai_model().invoke(_follow_up(stored_turn) + [answered, HumanMessage(content="thanks")])

    replayed = openai_api.bodies[0]["input"]
    assert "rs_old" not in json.dumps(replayed)
    function_call = next(item for item in replayed if item.get("type") == "function_call")
    assert "id" not in function_call and function_call["call_id"] == "call_1"
    assistant = next(item for item in replayed if item.get("role") == "assistant")
    assert "id" not in assistant


def test_openai_unreadable_reasoning_is_retried_once_without_it(openai_api, caplog) -> None:
    openai_api.answers = [_openai_stream(partial="", final=CIPHERTEXT), (400, REJECTION), _openai_text_stream()]
    model = _openai_model()
    ai = reduce(operator.add, model.stream([HumanMessage(content="look x up")]))

    result = reduce(operator.add, model.stream(_follow_up(ai)))

    assert result.text == "Done."
    rejected, retried = openai_api.bodies[1:]
    assert CIPHERTEXT in json.dumps(rejected)
    assert [item.get("type") for item in retried["input"]] == ["message", "function_call", "function_call_output"]
    assert "id" not in retried["input"][1]
    assert "replayed reasoning was rejected" in caplog.text


# --- Switching provider ---------------------------------------------------------------------------


def _history_from_responses_providers() -> list:
    """A thread that used Codex, xAI OAuth and direct OpenAI, each with a tool round."""
    def turn(call_id: str, **message: Any) -> list:
        return [
            AIMessage(tool_calls=[{"name": "lookup", "args": {}, "id": call_id, "type": "tool_call"}], **message),
            ToolMessage(content="ok", tool_call_id=call_id),
        ]

    item = {"type": "reasoning", "id": "rs_x", "summary": [], "encrypted_content": CIPHERTEXT}
    return [
        HumanMessage(content="first"),
        *turn("call_codex", content="", additional_kwargs=reasoning_replay_kwargs("codex", "gpt-5.5", [item])),
        *turn("call_xai", content="", additional_kwargs=reasoning_replay_kwargs("xai_oauth", "grok-4.6", [item])),
        *turn("call_openai", content=[dict(item), {"type": "function_call", "id": "fc_x", "call_id": "call_openai",
                                                    "name": "lookup", "arguments": "{}"}],
              response_metadata={"model_provider": "openai"}),
        HumanMessage(content="next"),
    ]


def _payload_for(target: str, messages: list) -> Any:
    if target == "anthropic":
        from row_bot.providers.transports.anthropic_cancellable import CancellableChatAnthropic

        return CancellableChatAnthropic(model="claude-sonnet-4-5", api_key="k")._get_request_payload(messages)
    if target == "google":
        from langchain_google_genai import ChatGoogleGenerativeAI

        return ChatGoogleGenerativeAI(model="gemini-2.5-flash", google_api_key="k")._prepare_request(messages)
    if target == "xai":
        from langchain_xai import ChatXAI

        return ChatXAI(model="grok-4", api_key="k")._get_request_payload(messages)
    if target == "openrouter":
        from row_bot.providers.transports.openrouter_cancellable import CancellableChatOpenRouter

        return CancellableChatOpenRouter(model_name="x/y", openrouter_api_key="k")._create_message_dicts(messages, None)
    if target == "openai_chat":
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(model="gpt-4.1", api_key="k")._get_request_payload(messages)
    if target == "custom_openai_lab":
        from row_bot.providers.transports.openai_compatible import ChatOpenAICompatible

        endpoint = {"provider_id": target, "base_url": "http://127.0.0.1:9/v1", "transport": "openai_chat",
                    "profile": "generic"}
        return ChatOpenAICompatible(model_name="m", api_key="k", base_url=endpoint["base_url"],
                                    endpoint=endpoint)._request_body(messages, stream=False)
    if target == "ollama_cloud":
        from row_bot.providers.transports.ollama_cloud import ChatOllamaCloud

        return ChatOllamaCloud(model_name="gpt-oss:120b", api_key="k")._request_body(messages, stream=False)
    if target == "codex":
        return ChatCodexResponses(model_name="gpt-5.4")._request_body(messages)
    if target == "xai_oauth":
        return ChatXAIOAuthResponses(model_name="grok-4.5", base_url="https://api.x.ai/v1")._request_body(messages)
    raise AssertionError(target)


@pytest.mark.parametrize("target", [
    "anthropic", "google", "xai", "openrouter", "openai_chat", "custom_openai_lab", "ollama_cloud", "codex", "xai_oauth",
])
def test_switching_provider_sends_no_foreign_reasoning(target) -> None:
    import row_bot.agent as agent

    messages = agent._normalize_provider_facing_messages(
        _history_from_responses_providers(),
        provider_id=target,
        anthropic_messages=target == "anthropic",
        google_genai=target == "google",
    )

    assert CIPHERTEXT not in json.dumps(_payload_for(target, messages), default=str)


def test_direct_openai_never_receives_subscription_reasoning() -> None:
    import row_bot.agent as agent
    from langchain_openai import ChatOpenAI

    history = [message for message in _history_from_responses_providers()
               if (message.response_metadata or {}).get("model_provider") != "openai"
               and getattr(message, "tool_call_id", "") != "call_openai"]
    messages = agent._normalize_provider_facing_messages(history, provider_id="openai")
    model = ChatOpenAI(model="gpt-5.4", api_key="k", use_responses_api=True, output_version="responses/v1", store=False)

    assert CIPHERTEXT not in json.dumps(model._get_request_payload(messages), default=str)
