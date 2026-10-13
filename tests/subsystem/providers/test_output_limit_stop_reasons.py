"""Every provider shape that says a reply hit the output limit is read the same way (F17)."""
from __future__ import annotations

import pytest
from langchain_core.messages import HumanMessage

from row_bot.providers.stop_reason import stop_reason, stopped_at_output_limit
from row_bot.providers.transports.codex_responses import ChatCodexResponses
from row_bot.providers.transports.ollama_cloud import ChatOllamaCloud
from row_bot.providers.transports.openai_compatible import ChatOpenAICompatible
from row_bot.providers.transports.xai_oauth_responses import ChatXAIOAuthResponses

pytestmark = pytest.mark.subsystem

RESPONSES_INCOMPLETE = [
    {"type": "response.output_text.delta", "delta": "The first half"},
    {"type": "response.incomplete", "response": {
        "id": "resp_fixture",
        "status": "incomplete",
        "incomplete_details": {"reason": "max_output_tokens"},
        "usage": {"input_tokens": 12, "output_tokens": 24, "total_tokens": 36},
    }},
]
RESPONSES_COMPLETED = [
    {"type": "response.output_text.delta", "delta": "All of it"},
    {"type": "response.completed", "response": {
        "id": "resp_fixture", "status": "completed",
        "usage": {"input_tokens": 12, "output_tokens": 3, "total_tokens": 15},
    }},
]


@pytest.mark.parametrize(
    ("metadata", "reason", "cut_short"),
    [
        ({"stop_reason": "max_tokens", "stop_sequence": None}, "max_tokens", True),  # Anthropic
        ({"stop_reason": "end_turn", "stop_sequence": None}, "end_turn", False),
        ({"finish_reason": "length"}, "length", True),  # Chat Completions, OpenAI-compatible
        ({"finish_reason": "tool_calls"}, "tool_calls", False),
        ({"done_reason": "length", "done": True}, "length", True),  # Ollama
        ({"finish_reason": "MAX_TOKENS", "model_name": "gemini-fixture"}, "max_tokens", True),  # Gemini
        ({"finish_reason": "STOP", "model_name": "gemini-fixture"}, "stop", False),
        ({"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}},
         "max_output_tokens", True),  # Responses APIs
        ({"status": "incomplete", "incomplete_details": {"reason": "content_filter"}}, "content_filter", False),
        ({"status": "completed"}, "", False),
        ({"token_usage": {"total_tokens": 3}}, "", False),
        (None, "", False),
    ],
)
def test_each_shape_gives_one_stop_reason(metadata, reason, cut_short):
    assert stop_reason(metadata) == reason
    assert stopped_at_output_limit(stop_reason(metadata)) is cut_short


def test_gemini_max_tokens_reaches_the_streamed_message():
    from google.genai import types as genai_types
    from langchain_core.language_models.chat_models import _gen_info_and_msg_metadata
    from langchain_google_genai.chat_models import _response_to_result

    recorded = genai_types.GenerateContentResponse.model_validate({
        "candidates": [{
            "content": {"role": "model", "parts": [{"text": "The first half"}]},
            "finish_reason": "MAX_TOKENS",
            "index": 0,
        }],
        "usage_metadata": {"prompt_token_count": 12, "candidates_token_count": 24, "total_token_count": 36},
        "model_version": "gemini-fixture",
    })
    [generation] = _response_to_result(recorded, stream=True).generations

    # LangChain folds generation_info into the streamed message's metadata.
    assert stop_reason(_gen_info_and_msg_metadata(generation)) == "max_tokens"


def test_openai_responses_incomplete_reaches_the_streamed_message():
    from langchain_openai.chat_models.base import _convert_responses_chunk_to_generation_chunk
    from openai.types.responses import ResponseIncompleteEvent

    recorded = ResponseIncompleteEvent.model_validate({
        "type": "response.incomplete",
        "sequence_number": 9,
        "response": {
            "id": "resp_fixture", "object": "response", "created_at": 1760000000, "model": "gpt-fixture",
            "status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"},
            "output": [], "parallel_tool_calls": True, "tool_choice": "auto", "tools": [],
            "usage": {
                "input_tokens": 12, "input_tokens_details": {"cached_tokens": 0},
                "output_tokens": 24, "output_tokens_details": {"reasoning_tokens": 0}, "total_tokens": 36,
            },
        },
    })
    _index, _output, _sub, chunk = _convert_responses_chunk_to_generation_chunk(recorded, 0, 0, 0)

    assert stop_reason(chunk.message.response_metadata) == "max_output_tokens"


@pytest.mark.parametrize("transport", [ChatCodexResponses, ChatXAIOAuthResponses])
def test_responses_transports_end_an_incomplete_reply_saying_why(monkeypatch, transport):
    monkeypatch.setattr(transport, "_iter_response_events", lambda self, body: iter(RESPONSES_INCOMPLETE))
    model = transport(model_name="fixture-model")

    chunks = list(model._stream([HumanMessage(content="hi")]))
    [generation] = model._generate([HumanMessage(content="hi")]).generations

    last = chunks[-1].message
    assert "".join(str(chunk.message.content) for chunk in chunks) == "The first half"
    assert last.chunk_position == "last"
    assert last.usage_metadata["output_tokens"] == 24
    assert stop_reason(last.response_metadata) == "max_output_tokens"
    assert stop_reason(generation.message.response_metadata) == "max_output_tokens"


@pytest.mark.parametrize("transport", [ChatCodexResponses, ChatXAIOAuthResponses])
def test_responses_transports_end_a_completed_reply_without_a_limit(monkeypatch, transport):
    monkeypatch.setattr(transport, "_iter_response_events", lambda self, body: iter(RESPONSES_COMPLETED))

    chunks = list(transport(model_name="fixture-model")._stream([HumanMessage(content="hi")]))

    assert chunks[-1].message.response_metadata["status"] == "completed"
    assert not stopped_at_output_limit(stop_reason(chunks[-1].message.response_metadata))


def _compatible(provider_id: str = "custom_openai_fixture", profile: str = "vllm",
                model_name: str = "fixture-model") -> ChatOpenAICompatible:
    return ChatOpenAICompatible(
        model_name=model_name,
        base_url="http://127.0.0.1:9/v1",
        endpoint={"provider_id": provider_id, "profile": profile, "transport": "openai_chat"},
    )


def test_openai_compatible_stream_says_it_hit_the_limit(monkeypatch):
    payloads = [
        {"choices": [{"index": 0, "delta": {"role": "assistant", "content": "The first half"}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "length"}]},
        {"choices": [], "usage": {"prompt_tokens": 12, "completion_tokens": 24, "total_tokens": 36}},
    ]
    monkeypatch.setattr(ChatOpenAICompatible, "_iter_stream_events", lambda self, body: iter(payloads))

    chunks = list(_compatible()._stream([HumanMessage(content="hi")]))

    assert chunks[-1].message.usage_metadata["output_tokens"] == 24
    assert stop_reason(chunks[-1].message.response_metadata) == "length"


def test_openai_compatible_anthropic_route_says_it_hit_the_limit(monkeypatch):
    payloads = [
        {"type": "message_start", "message": {"usage": {"input_tokens": 12, "output_tokens": 1}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "The first half"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "max_tokens", "stop_sequence": None},
         "usage": {"output_tokens": 24}},
        {"type": "message_stop"},
    ]
    monkeypatch.setattr(ChatOpenAICompatible, "_iter_stream_events", lambda self, body: iter(payloads))
    model = _compatible("atlascloud", "atlascloud", "anthropic/claude-fixture")

    chunks = list(model._stream([HumanMessage(content="hi")]))

    assert "".join(str(chunk.message.content) for chunk in chunks) == "The first half"
    assert stopped_at_output_limit(stop_reason(chunks[-1].message.response_metadata))


def test_openai_compatible_non_stream_reply_says_it_hit_the_limit(monkeypatch):
    class _Response:
        status_code = 200

        @staticmethod
        def json():
            return {
                "model": "fixture-model",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "The first half"},
                             "finish_reason": "length"}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 24, "total_tokens": 36},
            }

    monkeypatch.setattr(ChatOpenAICompatible, "_post", lambda self, body: _Response())
    monkeypatch.setattr(
        "row_bot.providers.transports.openai_compatible._endpoint_streaming_supported",
        lambda *args, **kwargs: False,
    )

    chunks = list(_compatible(profile="generic_openai")._stream([HumanMessage(content="hi")]))
    [generation] = _compatible(profile="generic_openai")._generate([HumanMessage(content="hi")]).generations

    assert stop_reason(chunks[-1].message.response_metadata) == "length"
    assert stop_reason(generation.message.response_metadata) == "length"


def test_ollama_cloud_stream_says_it_hit_the_limit(monkeypatch):
    payloads = [
        {"message": {"role": "assistant", "content": "The first half"}, "done": False},
        {"message": {"role": "assistant", "content": ""}, "done": True, "done_reason": "length",
         "prompt_eval_count": 12, "eval_count": 24},
    ]
    monkeypatch.setattr(ChatOllamaCloud, "_iter_stream_events", lambda self, body: iter(payloads))

    chunks = list(ChatOllamaCloud(model_name="gpt-oss:20b", api_key="key")._stream([HumanMessage(content="hi")]))

    assert stop_reason(chunks[-1].message.response_metadata) == "length"
