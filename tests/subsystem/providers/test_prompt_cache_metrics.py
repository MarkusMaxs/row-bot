from __future__ import annotations

import dataclasses
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage

_PAYLOADS = json.loads(
    (Path(__file__).resolve().parents[2] / "fixtures" / "provider_usage_payloads.json").read_text(encoding="utf-8")
)
_SHAPES = sorted(name for name in _PAYLOADS if not name.startswith("_"))


@pytest.mark.parametrize("shape", _SHAPES)
def test_every_recorded_provider_shape_normalizes_to_one_meaning(shape):
    from row_bot.prompt_cache import normalize_model_usage

    payload = _PAYLOADS[shape]
    usage = normalize_model_usage(payload["response_metadata"], payload["usage_metadata"])

    assert dataclasses.asdict(usage) == payload["expected"]


@pytest.mark.parametrize("shape", _SHAPES)
def test_transport_usage_metadata_round_trips_the_same_counts(shape):
    """What a custom transport puts on its closing chunk reads back unchanged."""
    from row_bot.prompt_cache import langchain_usage_metadata, normalize_model_usage

    usage = normalize_model_usage(_PAYLOADS[shape]["response_metadata"], _PAYLOADS[shape]["usage_metadata"])
    rebuilt = langchain_usage_metadata(usage)
    if usage.input_tokens is None and usage.output_tokens is None:
        assert rebuilt is None
        return
    again = normalize_model_usage({}, rebuilt)

    assert again.input_tokens == usage.input_tokens
    assert again.output_tokens == usage.output_tokens
    assert again.cache_read_tokens == usage.cache_read_tokens
    assert again.cache_write_tokens == usage.cache_write_tokens
    assert again.reasoning_tokens == usage.reasoning_tokens


def test_prompt_cache_counts_come_from_usage_metadata_even_beside_token_usage():
    from row_bot.prompt_cache import normalize_prompt_cache_usage

    payload = _PAYLOADS["token_usage_without_cache_beside_usage_metadata"]

    assert normalize_prompt_cache_usage(payload["response_metadata"]) == {"prompt_cache_read_tokens": 1920}
    assert normalize_prompt_cache_usage(
        {"token_usage": {"prompt_tokens": 10}},
        {"input_tokens": 10, "output_tokens": 1, "input_token_details": {"cache_read": 8, "cache_creation": 2}},
    ) == {"prompt_cache_read_tokens": 8, "prompt_cache_write_tokens": 2}


def test_malformed_usage_values_read_as_not_reported():
    from row_bot.prompt_cache import normalize_model_usage

    usage = normalize_model_usage({"token_usage": {
        "prompt_tokens": True,
        "completion_tokens": "many",
        "prompt_tokens_details": "cached",
        "cost": float("nan"),
    }})

    assert usage.input_tokens is None
    assert usage.output_tokens is None
    assert usage.cache_read_tokens is None
    assert usage.cost is None
    assert usage.reported is False


def test_prompt_cache_usage_normalizes_anthropic_and_openai_shapes():
    from row_bot.prompt_cache import normalize_prompt_cache_usage

    assert normalize_prompt_cache_usage({
        "token_usage": {
            "cache_read_input_tokens": 12,
            "cache_creation_input_tokens": 34,
        }
    }) == {
        "prompt_cache_read_tokens": 12,
        "prompt_cache_write_tokens": 34,
    }

    assert normalize_prompt_cache_usage({
        "token_usage": {
            "input_tokens_details": {"cached_tokens": "56"},
            "prompt_tokens_details": {"cache_creation_tokens": 78},
        }
    }) == {
        "prompt_cache_read_tokens": 56,
        "prompt_cache_write_tokens": 78,
    }


def test_stream_completion_diagnostics_log_prompt_cache_counts_without_prompt_text(monkeypatch, caplog):
    import row_bot.agent as agent

    monkeypatch.setattr(agent, "get_current_model", lambda: "model:anthropic:claude-sonnet-4-5")
    monkeypatch.setattr(agent, "get_context_size", lambda model=None: 200_000)
    monkeypatch.setattr(
        "row_bot.providers.resolution.resolve_provider_config",
        lambda *args, **kwargs: SimpleNamespace(
            provider_id="anthropic",
            runtime_model="claude-sonnet-4-5",
            selection_ref="model:anthropic:claude-sonnet-4-5",
        ),
    )

    latest = AIMessage(
        content="ok",
        response_metadata={
            "token_usage": {
                "cache_read_input_tokens": 12,
                "cache_creation_input_tokens": 34,
            },
            "debug_prompt_text": "SECRET_PROMPT_TEXT",
        },
    )

    with caplog.at_level(logging.INFO, logger="row_bot.agent"):
        agent._log_stream_completion(
            config={"configurable": {"thread_id": "thread-cache", "model_override": "model:anthropic:claude-sonnet-4-5"}},
            answer_chars=2,
            answer_chunks=1,
            reasoning_chars=0,
            reasoning_chunks=0,
            tool_call_count=0,
            tool_result_count=0,
            finish_reason="stop",
            stopped_by_user=False,
            loop_detected=False,
            browser_budget_exceeded=False,
            latest_ai_message=latest,
            phase_timings=None,
        )

    record = next(
        record for record in caplog.records
        if record.name == "row_bot.agent" and record.message.startswith("stream completion diagnostics=")
    )
    diagnostics = record.args[0] if isinstance(record.args, tuple) else record.args
    assert diagnostics["prompt_cache_read_tokens"] == 12
    assert diagnostics["prompt_cache_write_tokens"] == 34
    assert "SECRET_PROMPT_TEXT" not in record.message


def test_stream_completion_diagnostics_read_cache_counts_from_usage_metadata(monkeypatch, caplog):
    """Streamed usage arrives as usage_metadata only; the diagnostics still see it."""
    import row_bot.agent as agent

    monkeypatch.setattr(agent, "get_current_model", lambda: "model:openai:gpt-4.1")
    monkeypatch.setattr(agent, "get_context_size", lambda model=None: 200_000)
    monkeypatch.setattr(
        "row_bot.providers.resolution.resolve_provider_config",
        lambda *args, **kwargs: SimpleNamespace(
            provider_id="openai",
            runtime_model="gpt-4.1",
            selection_ref="model:openai:gpt-4.1",
        ),
    )
    latest = AIMessage(
        content="ok",
        response_metadata={"finish_reason": "stop"},
        usage_metadata={
            "input_tokens": 2006,
            "output_tokens": 3,
            "total_tokens": 2009,
            "input_token_details": {"cache_read": 1920},
        },
    )

    with caplog.at_level(logging.INFO, logger="row_bot.agent"):
        agent._log_stream_completion(
            config={"configurable": {"thread_id": "thread-cache", "model_override": "model:openai:gpt-4.1"}},
            answer_chars=2,
            answer_chunks=1,
            reasoning_chars=0,
            reasoning_chunks=0,
            tool_call_count=0,
            tool_result_count=0,
            finish_reason="stop",
            stopped_by_user=False,
            loop_detected=False,
            browser_budget_exceeded=False,
            latest_ai_message=latest,
            phase_timings=None,
        )

    record = next(
        record for record in caplog.records
        if record.name == "row_bot.agent" and record.message.startswith("stream completion diagnostics=")
    )
    diagnostics = record.args[0] if isinstance(record.args, tuple) else record.args
    assert diagnostics["prompt_cache_read_tokens"] == 1920
