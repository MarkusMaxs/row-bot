"""Preserved-thinking compliance on the direct Anthropic route (WP A2b).

A fake Messages API answers in-process through ``httpx.MockTransport``, so
these tests drive the real Anthropic SDK and langchain-anthropic request path
without any network.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import anthropic
import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

import row_bot.providers.runtime as runtime
from row_bot.providers.reasoning import ReasoningCapabilities, ReasoningRequestPlan, ReasoningSelection
from row_bot.providers.transports import anthropic_cancellable, thinking_binding
from row_bot.providers.transports import reasoning_fallback as fallback_module
from row_bot.providers.transports.anthropic_cancellable import CancellableChatAnthropic
from row_bot.providers.transports.reasoning_fallback import (
    ReasoningFallbackChatModel,
    should_retry_reasoning_error,
)

BETA = "thinking-binding-controls-2026-08-01"
DROP_BLOCK = {"prefix_mismatch_behavior": "drop_block"}
SIGNATURE_MISMATCH = (
    "messages.1.content.0: Invalid `signature` in `thinking` block. The block is bound to a "
    "different conversation. Remove the block, or set "
    '`thinking.block_binding.prefix_mismatch_behavior` to "drop_block".'
)
PRIVATE_THINKING = "private reasoning text"
PRIVATE_PROMPT = "private user prompt"


class _FakeMessagesAPI:
    """Answers Anthropic Messages requests in order and records what was sent."""

    def __init__(self, *answers: tuple[int, Any]) -> None:
        self.answers = list(answers)
        self.requests: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append({"body": body, "betas": request.headers.get("anthropic-beta", "")})
        status, answer = self.answers.pop(0)
        if status != 200:
            error = {"type": "error", "error": {"type": "invalid_request_error", "message": answer}}
            return httpx.Response(status, json=error)
        message = {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": body["model"],
            "content": [{"type": "text", "text": "done"}],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 3, "output_tokens": 1},
        }
        if answer is not None:
            message["input_transformations"] = answer
        if not body.get("stream"):
            return httpx.Response(200, json=message)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_sse(message),
        )


def _sse(message: dict[str, Any]) -> bytes:
    start = {**message, "content": [], "stop_reason": None, "usage": {"input_tokens": 3, "output_tokens": 0}}
    events = [
        {"type": "message_start", "message": start},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "done"}},
        {"type": "content_block_stop", "index": 0},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn", "stop_sequence": None},
            "usage": {"output_tokens": 1},
        },
        {"type": "message_stop"},
    ]
    return "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events).encode()


@pytest.fixture(autouse=True)
def _fresh_rejection_memory(monkeypatch):
    monkeypatch.setattr(thinking_binding, "_REJECTED_ROUTES", set())


@pytest.fixture
def fake_api(monkeypatch):
    """Route the production SDK clients of ``CancellableChatAnthropic`` to a fake API."""
    holder: dict[str, _FakeMessagesAPI] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        return holder["api"](request)

    transport = httpx.MockTransport(handler)
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


def _direct(model: str = "claude-opus-5-5", **kwargs: Any) -> CancellableChatAnthropic:
    return CancellableChatAnthropic(
        model=model,
        api_key="test-key",
        base_url="https://api.anthropic.test",
        max_retries=0,
        thinking_binding=True,
        **kwargs,
    )


def _history() -> list[Any]:
    """A conversation that replays thinking, redacted thinking, text and a tool round."""
    return [
        HumanMessage(content=PRIVATE_PROMPT),
        AIMessage(
            content=[
                {"type": "thinking", "thinking": PRIVATE_THINKING, "signature": "sig-1"},
                {"type": "text", "text": "Let me check."},
                {"type": "tool_use", "id": "toolu_1", "name": "lookup", "input": {"q": "x"}},
            ],
            tool_calls=[{"id": "toolu_1", "name": "lookup", "args": {"q": "x"}}],
        ),
        ToolMessage(content="result", tool_call_id="toolu_1"),
        AIMessage(
            content=[
                {"type": "redacted_thinking", "data": "opaque"},
                {"type": "text", "text": "The answer is x."},
            ]
        ),
        HumanMessage(content="And now?"),
    ]


def _block_types(body: dict[str, Any]) -> list[str]:
    return [
        block.get("type")
        for message in body["messages"]
        if isinstance(message.get("content"), list)
        for block in message["content"]
    ]


def _row_bot_log(caplog) -> str:
    """Row-Bot's own log lines (the SDK's DEBUG request dumps are not ours)."""
    return "\n".join(record.getMessage() for record in caplog.records if record.name.startswith("row_bot"))


def _anthropic_runtime(monkeypatch, model: str, *, reasoning_plan=None):
    monkeypatch.setattr(runtime, "get_provider_secret", lambda *_args, **_kwargs: "anthropic-key")
    return runtime.create_chat_model(model, provider_id="anthropic", reasoning_plan=reasoning_plan)


def _effort_plan(model: str, effort: str = "low") -> ReasoningRequestPlan:
    caps = ReasoningCapabilities(
        supported_efforts=("low", "medium", "high"),
        thinking_mode="adaptive",
        request_style="anthropic",
    )
    return ReasoningRequestPlan(f"model:anthropic:{model}", ReasoningSelection(kind="effort", effort=effort), caps)


# --- Attaching the controls ---------------------------------------------------------------


@pytest.mark.parametrize(
    "model",
    ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "claude-fable-5-1"],
)
def test_direct_anthropic_adaptive_requests_carry_drop_block_binding(monkeypatch, model) -> None:
    chat = _anthropic_runtime(monkeypatch, model)

    payload = chat._get_request_payload([HumanMessage(content="hello")])

    # These families run adaptive thinking when it is omitted; the payload says so
    # explicitly, because block_binding only rides on an adaptive thinking object.
    assert payload["thinking"] == {"type": "adaptive", "block_binding": DROP_BLOCK}
    assert payload["betas"] == [BETA]
    assert chat.thinking is None


def test_explicit_adaptive_thinking_keeps_its_fields_and_effort(monkeypatch) -> None:
    chat = _direct(thinking={"type": "adaptive", "display": "summarized"}, effort="low")

    payload = chat._get_request_payload([HumanMessage(content="hello")])

    assert payload["thinking"] == {"type": "adaptive", "display": "summarized", "block_binding": DROP_BLOCK}
    assert payload["output_config"] == {"effort": "low"}
    assert payload["betas"] == [BETA]
    assert chat.thinking == {"type": "adaptive", "display": "summarized"}


def test_reasoning_plan_on_direct_anthropic_carries_the_binding(monkeypatch) -> None:
    chat = _anthropic_runtime(monkeypatch, "claude-opus-5-5", reasoning_plan=_effort_plan("claude-opus-5-5"))

    assert isinstance(chat, ReasoningFallbackChatModel)
    for inner in (chat.primary, chat.provider_default):
        payload = inner._get_request_payload([HumanMessage(content="hello")])
        assert payload["thinking"]["block_binding"] == DROP_BLOCK
        assert payload["betas"] == [BETA]
    assert chat.primary._get_request_payload([HumanMessage(content="hi")])["output_config"] == {"effort": "low"}


def test_existing_betas_are_kept(fake_api) -> None:
    api = fake_api((200, []))
    chat = _direct(betas=["context-1m-2025-08-07"])

    payload = chat._get_request_payload([HumanMessage(content="hello")])
    chat.invoke([HumanMessage(content="hello")])

    assert payload["betas"] == ["context-1m-2025-08-07", BETA]
    assert api.requests[0]["betas"] == f"context-1m-2025-08-07,{BETA}"
    assert api.requests[0]["body"]["thinking"] == {"type": "adaptive", "block_binding": DROP_BLOCK}


def test_opus_4_8_without_thinking_gets_no_controls(monkeypatch) -> None:
    chat = _anthropic_runtime(monkeypatch, "claude-opus-4-8")

    payload = chat._get_request_payload([HumanMessage(content="hello")])

    # Omitting thinking means no thinking on Opus 4.8; it must stay off.
    assert "thinking" not in payload
    assert "betas" not in payload


@pytest.mark.parametrize(
    ("model", "thinking"),
    [
        ("claude-sonnet-5-5", {"type": "between_tools"}),
        ("claude-opus-5", {"type": "disabled"}),
        ("claude-sonnet-4-6", {"type": "enabled", "budget_tokens": 2048}),
    ],
)
def test_non_adaptive_thinking_is_left_alone(model, thinking) -> None:
    payload = _direct(model, thinking=thinking)._get_request_payload([HumanMessage(content="hello")])

    assert payload["thinking"] == thinking
    assert "betas" not in payload


def test_anthropic_format_gateways_get_no_controls(monkeypatch) -> None:
    monkeypatch.setattr(runtime, "get_provider_secret", lambda *_args, **_kwargs: "gateway-key")
    gateways = [
        runtime.create_chat_model("model:opencode_zen:claude-opus-5-5"),
        runtime.create_chat_model("claude-opus-5-5", provider_id="minimax"),
    ]

    for chat in gateways:
        assert isinstance(chat, CancellableChatAnthropic)
        assert chat.thinking_binding is False
        payload = chat._get_request_payload([HumanMessage(content="hello")])
        assert "thinking" not in payload
        assert "betas" not in payload


# --- Strip-and-retry ----------------------------------------------------------------------


def _call(chat, mode: str, messages):
    """Run one model call in ``mode`` and return the whole message."""
    if mode == "invoke":
        return chat.invoke(messages)
    if mode == "ainvoke":
        return asyncio.run(chat.ainvoke(messages))
    if mode == "stream":
        chunks = list(chat.stream(messages))
    else:

        async def collect():
            return [chunk async for chunk in chat.astream(messages)]

        chunks = asyncio.run(collect())
    merged = chunks[0]
    for chunk in chunks[1:]:
        merged = merged + chunk
    return merged


@pytest.mark.parametrize("mode", ["invoke", "stream", "ainvoke", "astream"])
def test_signature_mismatch_is_retried_once_without_thinking_blocks(fake_api, mode, caplog) -> None:
    api = fake_api((400, SIGNATURE_MISMATCH), (200, []))
    caplog.set_level(logging.DEBUG, logger="row_bot")

    result = _call(_direct(), mode, _history())

    assert "done" in str(result.content)
    assert len(api.requests) == 2
    first, retry = (request["body"] for request in api.requests)
    assert "thinking" in _block_types(first) and "redacted_thinking" in _block_types(first)
    assert _block_types(retry) == ["text", "tool_use", "tool_result", "text"]
    assert retry["messages"][1]["content"][0] == {"type": "text", "text": "Let me check."}
    # The controls were not the problem, so the retry keeps them.
    assert retry["thinking"]["block_binding"] == DROP_BLOCK
    assert api.requests[1]["betas"] == BETA
    row_bot_log = _row_bot_log(caplog)
    assert "retrying once without 2 thinking block(s)" in row_bot_log
    assert PRIVATE_THINKING not in row_bot_log
    assert PRIVATE_PROMPT not in row_bot_log


def test_signature_mismatch_is_retried_at_most_once(fake_api) -> None:
    api = fake_api((400, SIGNATURE_MISMATCH), (400, SIGNATURE_MISMATCH))

    with pytest.raises(anthropic.BadRequestError, match="bound to a different conversation"):
        _direct().invoke(_history())

    assert len(api.requests) == 2


@pytest.mark.parametrize(
    "message",
    [
        "Unexpected value(s) `thinking-binding-controls-2026-08-01` for the `anthropic-beta` header.",
        "thinking.adaptive.block_binding: Extra inputs are not permitted",
    ],
)
def test_rejected_controls_are_dropped_with_the_thinking_blocks(fake_api, message) -> None:
    api = fake_api((400, message), (200, []), (200, []))
    chat = _direct()

    chat.invoke(_history())
    chat.invoke(_history())

    assert len(api.requests) == 3
    retry = api.requests[1]
    assert retry["betas"] == ""
    assert "block_binding" not in retry["body"]["thinking"]
    assert "thinking" not in _block_types(retry["body"])
    # The route remembers the rejection: the next call goes out once, without the
    # controls and with its thinking blocks intact.
    later = api.requests[2]
    assert later["betas"] == ""
    assert "thinking" not in later["body"]
    assert "thinking" in _block_types(later["body"])


@pytest.mark.parametrize(
    "message",
    [
        "messages.0.content.1: tool_use ids must be unique",
        '"thinking.type.disabled" is not supported for this model.',
        "prompt is too long: 1200000 tokens > 1000000 maximum",
    ],
)
def test_unrelated_bad_requests_are_not_retried(fake_api, message) -> None:
    api = fake_api((400, message))

    with pytest.raises(anthropic.BadRequestError):
        _direct().invoke(_history())

    assert len(api.requests) == 1


def test_gateways_never_strip_and_retry(fake_api) -> None:
    api = fake_api((400, SIGNATURE_MISMATCH))
    gateway = CancellableChatAnthropic(
        model="claude-opus-5-5", api_key="test-key", base_url="https://gateway.test", max_retries=0
    )

    with pytest.raises(anthropic.BadRequestError):
        gateway.invoke(_history())

    assert len(api.requests) == 1


def test_reasoning_fallback_leaves_the_retry_to_the_transport(fake_api, monkeypatch) -> None:
    api = fake_api((400, SIGNATURE_MISMATCH), (200, []))
    notices: list[Any] = []
    monkeypatch.setattr(fallback_module, "queue_reasoning_notice", lambda *args: notices.append(args))
    monkeypatch.setattr(fallback_module, "suppress_reasoning_override", lambda *args: notices.append(args))
    chat = _anthropic_runtime(monkeypatch, "claude-opus-5-5", reasoning_plan=_effort_plan("claude-opus-5-5"))

    result = chat.invoke(_history())

    assert result.content == "done"
    assert len(api.requests) == 2
    # Both requests keep the user's effort: the transport's retry is not a reasoning fallback.
    assert [request["body"]["output_config"] for request in api.requests] == [{"effort": "low"}] * 2
    assert notices == []


def test_a_failed_strip_and_retry_is_not_followed_by_a_reasoning_fallback(fake_api, monkeypatch) -> None:
    api = fake_api((400, SIGNATURE_MISMATCH), (400, SIGNATURE_MISMATCH))
    resets: list[Any] = []
    monkeypatch.setattr(fallback_module, "suppress_reasoning_override", lambda *args: resets.append(args))
    chat = _anthropic_runtime(monkeypatch, "claude-opus-5-5", reasoning_plan=_effort_plan("claude-opus-5-5"))

    with pytest.raises(anthropic.BadRequestError):
        chat.invoke(_history())

    # One request and one stripped retry; Provider default would only repeat it.
    assert len(api.requests) == 2
    assert resets == []


def test_reasoning_fallback_never_retries_a_binding_error() -> None:
    request = httpx.Request("POST", "https://api.anthropic.test/v1/messages")
    for message in (SIGNATURE_MISMATCH, "thinking.adaptive.block_binding: Extra inputs are not permitted"):
        error = anthropic.BadRequestError(
            message,
            response=httpx.Response(400, request=request),
            body={"type": "error", "error": {"type": "invalid_request_error", "message": message}},
        )
        assert should_retry_reasoning_error(error) is False


# --- input_transformations ----------------------------------------------------------------

DROPS = [
    {"type": "thinking_dropped", "path": "messages.1.content.0", "reason": "prefix_binding_mismatch"},
    {"type": "thinking_dropped", "path": "messages.3.content.0", "reason": "prefix_binding_mismatch"},
]


@pytest.mark.parametrize("mode", ["invoke", "stream"])
def test_dropped_thinking_blocks_are_logged_by_path_and_reason(fake_api, mode, caplog) -> None:
    fake_api((200, DROPS))
    caplog.set_level(logging.INFO, logger=thinking_binding.__name__)

    result = _call(_direct(), mode, _history())

    assert result.response_metadata["input_transformations"] == DROPS
    records = [record for record in caplog.records if record.name == thinking_binding.__name__]
    assert [record.levelno for record in records] == [logging.INFO]
    line = records[0].getMessage()
    assert "dropped 2 thinking block(s)" in line
    assert "messages.1.content.0:prefix_binding_mismatch" in line
    assert "messages.3.content.0:prefix_binding_mismatch" in line
    assert PRIVATE_THINKING not in _row_bot_log(caplog)
    assert PRIVATE_PROMPT not in _row_bot_log(caplog)


def test_an_empty_transformation_list_logs_nothing(fake_api, caplog) -> None:
    fake_api((200, []))
    caplog.set_level(logging.DEBUG, logger=thinking_binding.__name__)

    chunks = list(_direct().stream(_history()))

    assert not [record for record in caplog.records if record.name == thinking_binding.__name__]
    assert all("input_transformations" not in chunk.response_metadata for chunk in chunks)
