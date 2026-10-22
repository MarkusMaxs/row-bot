"""A provider's own reason for refusing a request reaches the person, on a streamed request too (B393).

Row-Bot's transports stream with httpx, whose streamed answer has no text until its body is read. The provider
here is a real HTTP endpoint on loopback, so a refusal arrives exactly as it does from the provider.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Callable

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from row_bot.providers.transports.responses_reasoning import reasoning_replay_kwargs
from tests.helpers.fake_provider_http import Reply, fake_provider_http

pytestmark = pytest.mark.subsystem

PROMPT = [HumanMessage(content="What is 2 + 2?")]
REASON = "This request names a model the account can't use."


def _codex(url: str, monkeypatch: pytest.MonkeyPatch, model: str = "gpt-test") -> Any:
    from row_bot.providers import codex
    from row_bot.providers.transports.codex_responses import ChatCodexResponses

    monkeypatch.setattr(codex, "codex_runtime_credentials",
                        lambda **_kwargs: SimpleNamespace(access_token="t", account_id="a", refresh_token=""))
    return ChatCodexResponses(model_name=model, base_url=url)


def _ollama_cloud(url: str, _monkeypatch: pytest.MonkeyPatch) -> Any:
    from row_bot.providers.transports.ollama_cloud import ChatOllamaCloud

    return ChatOllamaCloud(model_name="m", api_key="k", base_url=url)


def _openai_compatible(url: str, _monkeypatch: pytest.MonkeyPatch) -> Any:
    from row_bot.providers.transports.openai_compatible import ChatOpenAICompatible

    endpoint = {"provider_id": "custom_reason_test", "base_url": f"{url}/v1", "transport": "openai_chat"}
    return ChatOpenAICompatible(model_name="m", api_key="k", base_url=f"{url}/v1", endpoint=endpoint)


def _xai_sign_in(url: str, monkeypatch: pytest.MonkeyPatch) -> Any:
    from row_bot.providers import xai_oauth
    from row_bot.providers.transports.xai_oauth_responses import ChatXAIOAuthResponses

    monkeypatch.setattr(xai_oauth, "xai_oauth_runtime_credentials", lambda **_kwargs: SimpleNamespace(access_token="t"))
    monkeypatch.setattr(xai_oauth, "xai_oauth_base_url", lambda override=None: override)
    return ChatXAIOAuthResponses(model_name="grok-test", base_url=url)


@dataclass(frozen=True)
class Refusal:
    build: Callable[[str, pytest.MonkeyPatch], Any]
    body: dict


# Each provider's own shape for an error.
REFUSALS = {
    "codex": Refusal(_codex, {"detail": REASON}),
    "ollama_cloud": Refusal(_ollama_cloud, {"error": REASON}),
    "openai_compatible": Refusal(_openai_compatible, {"error": {"message": REASON, "type": "invalid_request_error"}}),
    "xai_sign_in": Refusal(_xai_sign_in, {"error": {"message": REASON}}),
}


@pytest.mark.parametrize("way", ["stream", "invoke"])
@pytest.mark.parametrize("name", REFUSALS)
def test_a_refused_request_says_why(name: str, way: str, monkeypatch: pytest.MonkeyPatch) -> None:
    refusal = REFUSALS[name]
    with fake_provider_http([Reply(body=json.dumps(refusal.body).encode(), status=400)]) as server:
        model = refusal.build(server.url, monkeypatch)
        with pytest.raises(RuntimeError) as raised:
            if way == "stream":
                list(model.stream(PROMPT))
            else:
                model.invoke(PROMPT)

    assert REASON in str(raised.value)


UNSUPPORTED = "The 'gpt-5.5' model is not supported when using Codex with a ChatGPT account."


@pytest.mark.parametrize("refusal", [
    (400, UNSUPPORTED),
    (404, "The model `gpt-5.5` does not exist or you do not have access to it."),
], ids=["not-supported", "does-not-exist"])
def test_a_model_chatgpt_no_longer_offers_is_named_plainly(refusal: tuple, monkeypatch: pytest.MonkeyPatch) -> None:
    import row_bot.agent as agent
    from row_bot.providers.transports.reasoning_fallback import should_retry_reasoning_error

    status, detail = refusal
    with fake_provider_http([Reply(body=json.dumps({"detail": detail}).encode(), status=status)]) as server:
        with pytest.raises(RuntimeError) as raised:
            _codex(server.url, monkeypatch, model="gpt-5.5").invoke(PROMPT)

    message = str(raised.value)
    assert message == f"gpt-5.5 isn't available with your ChatGPT plan. Choose another model. ChatGPT said: {detail}"
    # A chat or a workflow run shows it as it is, and it isn't taken for a reasoning setting to drop.
    assert agent._friendly_api_error(message, "gpt-5.5") == f"⚠️ {message}"
    assert should_retry_reasoning_error(raised.value) is False


def test_a_refused_setting_is_not_taken_for_a_missing_model(monkeypatch: pytest.MonkeyPatch) -> None:
    detail = "The model does not support reasoning summaries; 'summary' is not supported."
    with fake_provider_http([Reply(body=json.dumps({"detail": detail}).encode(), status=400)]) as server:
        with pytest.raises(RuntimeError) as raised:
            _codex(server.url, monkeypatch, model="gpt-5.5").invoke(PROMPT)

    assert str(raised.value) == f"Codex Responses request failed with HTTP 400: {detail}"


REPLAYED_ITEM = {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "gAAAA-ciphertext"}
TOOL = {"type": "function", "function": {
    "name": "lookup", "description": "Look something up.",
    "parameters": {"type": "object", "properties": {"q": {"type": "string"}}},
}}
ANSWER = "".join(
    f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in (
        {"type": "response.output_text.delta", "delta": "x is 42."},
        {"type": "response.completed", "response": {"id": "r2", "status": "completed", "output": []}},
    )
).encode()


def test_chatgpt_retries_without_reasoning_it_can_no_longer_read(monkeypatch: pytest.MonkeyPatch) -> None:
    rejection = {"error": {"message": "The encrypted content for item rs_1 could not be verified."}}
    replies = [Reply(body=json.dumps(rejection).encode(), status=400),
               Reply(body=ANSWER, content_type="text/event-stream")]
    turn = [
        HumanMessage(content="look x up"),
        AIMessage(content="", tool_calls=[{"id": "call_1", "name": "lookup", "args": {"q": "x"}}],
                  additional_kwargs=reasoning_replay_kwargs("codex", "gpt-test", [REPLAYED_ITEM])),
        ToolMessage(content="x is 42", tool_call_id="call_1"),
    ]
    with fake_provider_http(replies) as server:
        answer = _codex(server.url, monkeypatch).bind_tools([TOOL]).invoke(turn)

    assert answer.content == "x is 42."
    sent = [[item.get("type") for item in json.loads(request.body)["input"]] for request in server.seen]
    assert sent == [
        ["message", "reasoning", "function_call", "function_call_output"],
        ["message", "function_call", "function_call_output"],
    ]
