"""Each provider gets one stable cache-routing key per conversation (WP B2).

Requests go through the real SDKs and Row-Bot transports; HTTP is answered in
process, so these tests read exactly what would reach the provider.
"""

from __future__ import annotations

import contextvars
import json
import uuid
from types import SimpleNamespace
from typing import Any, Callable

import httpx
import pytest
from langchain_core.messages import HumanMessage

import row_bot.providers.runtime as runtime
from row_bot.providers import cache_affinity
from row_bot.providers.transports.codex_responses import ChatCodexResponses
from row_bot.providers.transports.xai_oauth_responses import ChatXAIOAuthResponses

PROMPT = [HumanMessage(content="Reply with OK")]
# Channel conversation ids embed chat ids and phone numbers.
CONVERSATION = "telegram:12345:15550001111"
OTHER_CONVERSATION = "conversation-b"


def in_conversation(thread_id: str, call: Callable[[], Any]) -> Any:
    """Run ``call`` as the agent does inside a conversation (or outside one for ``""``)."""
    import row_bot.agent as agent

    def run() -> Any:
        agent._set_active_runtime_context(thread_id=thread_id)
        return call()

    return contextvars.copy_context().run(run)


@pytest.fixture
def sent(monkeypatch) -> list[httpx.Request]:
    """Answer every SDK HTTP request in process, by protocol, and record it."""
    requests: list[httpx.Request] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/responses"):
            body = {"id": "resp_1", "object": "response", "created_at": 0, "model": "m", "status": "completed",
                    "output": [{"type": "message", "id": "msg_1", "role": "assistant", "status": "completed",
                                "content": [{"type": "output_text", "text": "OK", "annotations": []}]}],
                    "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                    "parallel_tool_calls": True, "tool_choice": "auto", "tools": []}
        else:
            body = {"id": "chat-1", "object": "chat.completion", "created": 0, "model": "m",
                    "system_fingerprint": None, "choices": [
                {"index": 0, "message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"},
            ], "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}
        return httpx.Response(200, json=body, request=request)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", handle_request)
    monkeypatch.setattr(runtime, "get_provider_secret", lambda provider_id, *args, **kwargs: "test-key")
    return requests


def _bodies(requests: list[httpx.Request]) -> list[dict]:
    return [json.loads(request.content) for request in requests]


def _ask_in(model: Any, *conversations: str) -> None:
    for thread_id in conversations:
        in_conversation(thread_id, lambda: model.invoke(PROMPT))


def _assert_one_key_per_conversation(keys: list[Any]) -> None:
    first, again, other, outside = keys
    assert first and first == again
    assert other and other != first
    assert outside is None
    assert uuid.UUID(first).version == 4
    assert "12345" not in first and "15550001111" not in first


def test_the_key_is_per_conversation_per_provider_and_opaque() -> None:
    def keys(scope: str) -> list[str | None]:
        return [in_conversation(thread, lambda: cache_affinity.conversation_cache_key(scope))
                for thread in (CONVERSATION, CONVERSATION, OTHER_CONVERSATION, "")]

    openai_keys, xai_keys = keys("openai"), keys("xai")

    _assert_one_key_per_conversation(openai_keys)
    _assert_one_key_per_conversation(xai_keys)
    # Two providers can't link one conversation by its key.
    assert openai_keys[0] != xai_keys[0]


def test_openai_responses_send_the_conversation_key_and_store_nothing(sent) -> None:
    model = runtime.create_chat_model("gpt-5.4", provider_id="openai")

    _ask_in(model, CONVERSATION, CONVERSATION, OTHER_CONVERSATION, "")

    bodies = _bodies(sent)
    assert [request.url.path for request in sent] == ["/v1/responses"] * 4
    _assert_one_key_per_conversation([body.get("prompt_cache_key") for body in bodies])
    assert all(body["store"] is False for body in bodies)
    assert all(body["include"] == ["reasoning.encrypted_content"] for body in bodies)
    # Retention stays at the server default.
    assert not any("prompt_cache_retention" in body for body in bodies)


def test_openai_chat_completions_send_the_conversation_key(sent) -> None:
    model = runtime.create_chat_model("gpt-4.1", provider_id="openai")

    _ask_in(model, CONVERSATION, CONVERSATION, OTHER_CONVERSATION, "")

    bodies = _bodies(sent)
    assert [request.url.path for request in sent] == ["/v1/chat/completions"] * 4
    _assert_one_key_per_conversation([body.get("prompt_cache_key") for body in bodies])
    assert not any("store" in body or "include" in body for body in bodies)


def test_xai_chat_completions_send_the_conversation_header(sent) -> None:
    model = runtime.create_chat_model("grok-4", provider_id="xai")

    _ask_in(model, CONVERSATION, CONVERSATION, OTHER_CONVERSATION, "")

    assert [request.url.path for request in sent] == ["/v1/chat/completions"] * 4
    _assert_one_key_per_conversation([request.headers.get("x-grok-conv-id") for request in sent])
    assert not any("prompt_cache_key" in body for body in _bodies(sent))


def test_openrouter_keeps_a_conversation_on_one_upstream_provider(sent) -> None:
    model = runtime.create_chat_model("anthropic/claude-sonnet-4", provider_id="openrouter")

    _ask_in(model, CONVERSATION, CONVERSATION, OTHER_CONVERSATION, "")

    bodies = _bodies(sent)
    assert [request.url.path for request in sent] == ["/api/v1/chat/completions"] * 4
    _assert_one_key_per_conversation([body.get("session_id") for body in bodies])
    # A provider order would turn sticky routing off.
    assert not any("provider" in body for body in bodies)


class _SSEClient:
    """Answers each Responses request with one streamed text reply."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def post(self, url: str, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        lines = [
            "event: response.output_text.delta", 'data: {"delta": "OK"}', "",
            "event: response.completed", 'data: {"response": {"id": "resp_1"}}', "",
        ]
        return SimpleNamespace(status_code=200, text="", iter_lines=lambda: iter(line.encode() for line in lines))

    def close(self) -> None:
        pass


@pytest.fixture
def subscription_credentials(monkeypatch) -> None:
    from row_bot.providers import codex, xai_oauth

    credentials = SimpleNamespace(access_token="access", account_id="account", refresh_token="")
    monkeypatch.setattr(codex, "codex_runtime_credentials", lambda **kwargs: credentials)
    monkeypatch.setattr(xai_oauth, "xai_oauth_runtime_credentials", lambda **kwargs: credentials)


def test_codex_session_is_the_conversation_key_for_every_model_instance(subscription_credentials) -> None:
    client = _SSEClient()

    # Chat Only and compaction build a new instance per call.
    for thread_id in (CONVERSATION, CONVERSATION, OTHER_CONVERSATION, ""):
        model = ChatCodexResponses(model_name="gpt-5.5", http_client=client)
        in_conversation(thread_id, lambda: model.invoke(PROMPT))

    body_keys = [call["json"]["prompt_cache_key"] for call in client.calls]
    header_keys = [call["headers"]["session_id"] for call in client.calls]
    assert body_keys == header_keys
    first, again, other, outside = body_keys
    assert first == again != other
    assert outside not in {first, other}
    assert first == in_conversation(CONVERSATION, lambda: cache_affinity.conversation_cache_key("codex"))
    assert "12345" not in first


def test_codex_outside_a_conversation_keeps_one_session_per_instance(subscription_credentials) -> None:
    client = _SSEClient()
    model = ChatCodexResponses(model_name="gpt-5.5", http_client=client)

    _ask_in(model, "", "")

    sessions = {call["headers"]["session_id"] for call in client.calls}
    assert sessions == {call["json"]["prompt_cache_key"] for call in client.calls}
    assert len(sessions) == 1


def test_xai_oauth_responses_send_the_conversation_key(subscription_credentials) -> None:
    client = _SSEClient()
    model = ChatXAIOAuthResponses(model_name="grok-4.6", base_url="https://api.x.ai/v1", http_client=client)

    _ask_in(model, CONVERSATION, CONVERSATION, OTHER_CONVERSATION, "")

    bodies = [call["json"] for call in client.calls]
    _assert_one_key_per_conversation([body.get("prompt_cache_key") for body in bodies])
    assert all(body["include"] == ["reasoning.encrypted_content"] for body in bodies)


def test_xai_oauth_non_reasoning_models_are_not_asked_for_encrypted_reasoning(subscription_credentials) -> None:
    client = _SSEClient()
    model = ChatXAIOAuthResponses(
        model_name="grok-4-fast-non-reasoning", base_url="https://api.x.ai/v1", http_client=client,
    )

    _ask_in(model, CONVERSATION)

    assert "include" not in client.calls[0]["json"]
