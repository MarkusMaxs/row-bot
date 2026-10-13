"""A reply cut off by the output token limit says so, on every provider shape (F17)."""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage

pytestmark = pytest.mark.subsystem

CUT_SHORT = "cut short by the model's output token limit"


def _stream(response_metadata: dict) -> list:
    import row_bot.agent as agent

    class FakeState:
        next: list = []
        tasks: list = []
        values = {"messages": [HumanMessage(content="write it all"), AIMessage(content="The first half")]}

    class FakeGraph:
        def stream(self, input_data, config=None, stream_mode=None):
            meta = {"langgraph_node": "agent", "run_id": "provider-call-1"}
            yield ("messages", (AIMessageChunk(content="The first half"), meta))
            # The final chunk says why the provider stopped.
            yield ("messages", (AIMessageChunk(content="", response_metadata=response_metadata), meta))

        def get_state(self, config):
            return FakeState()

    return list(agent._stream_graph(
        FakeGraph(),
        {"messages": [("human", "write it all")]},
        {"configurable": {"thread_id": "thread-limit", "generation_id": "thread-limit:gen-1"}},
    ))


@pytest.mark.parametrize(
    "response_metadata",
    [
        {"stop_reason": "max_tokens", "stop_sequence": None},  # Anthropic, streamed message_delta
        {"finish_reason": "length"},  # OpenAI-style
        {"done_reason": "length"},  # Ollama
        {"finish_reason": "MAX_TOKENS", "model_name": "gemini-fixture", "safety_ratings": []},  # Gemini
        # Responses APIs (OpenAI, Codex, xAI OAuth): no stop reason, an incomplete status instead.
        {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}, "model_name": "gpt-fixture"},
    ],
)
def test_a_reply_that_hit_the_output_limit_tells_the_person(response_metadata):
    events = _stream(response_metadata)

    [done] = [payload for event_type, payload in events if event_type == "done"]
    assert done.startswith("The first half")
    assert CUT_SHORT in done


@pytest.mark.parametrize(
    "response_metadata",
    [
        {"stop_reason": "end_turn", "stop_sequence": None},
        {"finish_reason": "stop"},
        {"finish_reason": "STOP", "model_name": "gemini-fixture"},
        {"status": "completed", "model_name": "gpt-fixture"},
        {"status": "incomplete", "incomplete_details": {"reason": "content_filter"}},
    ],
)
def test_a_reply_that_finished_normally_has_no_notice(response_metadata):
    events = _stream(response_metadata)

    assert [payload for event_type, payload in events if event_type == "done"] == ["The first half"]
