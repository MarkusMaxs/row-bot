"""Row-Bot's own transports end a stream with the usage the provider reported."""
from __future__ import annotations

import sqlite3
from contextlib import closing

import pytest
from langchain_core.messages import HumanMessage

from row_bot.providers.transports.codex_responses import ChatCodexResponses
from row_bot.providers.transports.ollama_cloud import ChatOllamaCloud
from row_bot.providers.transports.openai_compatible import ChatOpenAICompatible
from row_bot.providers.transports.xai_oauth_responses import ChatXAIOAuthResponses
from row_bot.providers.usage_ledger import attach_usage_ledger

pytestmark = pytest.mark.subsystem

RESPONSES_EVENTS = [
    {"type": "response.output_text.delta", "delta": "Hel"},
    {"type": "response.output_text.delta", "delta": "lo"},
    {"type": "response.completed", "response": {
        "id": "resp_fixture",
        "usage": {
            "input_tokens": 5000,
            "input_tokens_details": {"cached_tokens": 4096},
            "output_tokens": 800,
            "output_tokens_details": {"reasoning_tokens": 512},
            "total_tokens": 5800,
        },
    }},
]


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path))
    return tmp_path / "usage.db"


def _ledger_counts(path) -> list[tuple]:
    with closing(sqlite3.connect(path)) as connection:
        return connection.execute(
            "SELECT provider, input_tokens, cache_read_tokens, output_tokens, reasoning_tokens FROM model_usage"
        ).fetchall()


@pytest.mark.parametrize(
    ("transport", "provider"),
    [(ChatCodexResponses, "codex"), (ChatXAIOAuthResponses, "xai_oauth")],
)
def test_responses_transports_close_the_stream_with_completed_usage(monkeypatch, ledger, transport, provider):
    monkeypatch.setattr(transport, "_iter_response_events", lambda self, body: iter(RESPONSES_EVENTS))
    model = attach_usage_ledger(transport(model_name="fixture-model"), provider_id=provider, model_name="fixture-model")

    chunks = list(model._stream([HumanMessage(content="hi")]))
    streamed = list(model.stream([HumanMessage(content="hi")]))

    last = chunks[-1].message
    assert last.chunk_position == "last"
    assert last.usage_metadata == {
        "input_tokens": 5000,
        "output_tokens": 800,
        "total_tokens": 5800,
        "input_token_details": {"cache_read": 4096},
        "output_token_details": {"reasoning": 512},
    }
    assert "".join(str(chunk.content) for chunk in streamed) == "Hello"
    assert _ledger_counts(ledger) == [(provider, 5000, 4096, 800, 512)]


def test_responses_transports_without_a_completed_event_still_close_the_stream(monkeypatch):
    monkeypatch.setattr(ChatCodexResponses, "_iter_response_events", lambda self, body: iter(RESPONSES_EVENTS[:2]))

    chunks = list(ChatCodexResponses(model_name="fixture-model")._stream([HumanMessage(content="hi")]))

    assert chunks[-1].message.chunk_position == "last"
    assert chunks[-1].message.usage_metadata is None


def test_ollama_cloud_keeps_the_done_payload_counts(monkeypatch, ledger):
    payloads = [
        {"message": {"role": "assistant", "content": "Hi"}, "done": False},
        {"message": {"role": "assistant", "content": ""}, "done": True, "done_reason": "stop",
         "prompt_eval_count": 640, "eval_count": 88, "total_duration": 1},
    ]
    monkeypatch.setattr(ChatOllamaCloud, "_iter_stream_events", lambda self, body: iter(payloads))
    model = attach_usage_ledger(
        ChatOllamaCloud(model_name="gpt-oss:20b", api_key="key"),
        provider_id="ollama_cloud",
        model_name="gpt-oss:20b",
    )

    chunks = list(model._stream([HumanMessage(content="hi")]))
    list(model.stream([HumanMessage(content="hi")]))

    last = chunks[-1].message
    assert last.usage_metadata == {"input_tokens": 640, "output_tokens": 88, "total_tokens": 728}
    assert last.response_metadata["prompt_eval_count"] == 640
    assert last.response_metadata["done_reason"] == "stop"  # why it stopped, for the cut-short notice (F17)
    assert _ledger_counts(ledger) == [("ollama_cloud", 640, None, 88, None)]


def _compatible(profile: str) -> ChatOpenAICompatible:
    return ChatOpenAICompatible(
        model_name="fixture-model",
        base_url="http://127.0.0.1:9/v1",
        endpoint={"provider_id": "custom_openai_fixture", "profile": profile, "transport": "openai_chat"},
    )


@pytest.mark.parametrize(
    ("profile", "asks"),
    [("vllm", True), ("sglang", True), ("litellm", True), ("generic_openai", False), ("llama_cpp", False),
     ("lmstudio", False), ("atlascloud", False)],
)
def test_stream_usage_is_requested_only_where_the_profile_accepts_it(profile, asks):
    model = _compatible(profile)

    streamed = model._request_body([HumanMessage(content="hi")], stream=True)
    single = model._request_body([HumanMessage(content="hi")], stream=False)

    assert ("stream_options" in streamed) is asks
    if asks:
        assert streamed["stream_options"] == {"include_usage": True}
    assert "stream_options" not in single


def test_openai_compatible_stream_reads_the_final_usage_chunk(monkeypatch, ledger):
    payloads = [
        {"choices": [{"index": 0, "delta": {"role": "assistant", "content": "Hi"}}], "usage": None},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}], "usage": None},
        {"choices": [], "usage": {
            "prompt_tokens": 3000,
            "completion_tokens": 200,
            "total_tokens": 3200,
            "prompt_cache_hit_tokens": 2560,
            "prompt_cache_miss_tokens": 440,
        }},
    ]
    monkeypatch.setattr(ChatOpenAICompatible, "_iter_stream_events", lambda self, body: iter(payloads))
    model = attach_usage_ledger(_compatible("vllm"), provider_id="custom_openai_fixture", model_name="fixture-model")

    chunks = list(model._stream([HumanMessage(content="hi")]))
    list(model.stream([HumanMessage(content="hi")]))

    last = chunks[-1].message
    assert last.usage_metadata["input_tokens"] == 3000
    assert last.usage_metadata["input_token_details"] == {"cache_read": 2560}
    assert _ledger_counts(ledger) == [("custom_openai_fixture", 3000, 2560, 200, None)]


def test_openai_compatible_non_stream_fallback_keeps_the_usage(monkeypatch):
    class _Response:
        status_code = 200

        @staticmethod
        def json():
            return {
                "model": "fixture-model",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "Hi"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15},
            }

    monkeypatch.setattr(ChatOpenAICompatible, "_post", lambda self, body: _Response())
    monkeypatch.setattr(
        "row_bot.providers.transports.openai_compatible._endpoint_streaming_supported",
        lambda *args, **kwargs: False,
    )

    chunks = list(_compatible("generic_openai")._stream([HumanMessage(content="hi")]))

    assert chunks[-1].message.usage_metadata == {"input_tokens": 12, "output_tokens": 3, "total_tokens": 15}
