"""Compaction comes near the limit, after old tool output is cleared (WP A4b, decision D2).

Row-Bot compacted at 75% of the window. It now does what Claude Code does: old tool output is
cleared first (A4a's sticky jump), and history is summarized only when the request is still within
the reply and summary allowances of the window. These cases check the trigger per window size, the
order (clear, then summarize), and that the comparison uses the calibrated estimate.
"""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, HumanMessage

import row_bot.agent as agent
from row_bot import context_calibration, models
from tests.helpers.agent_requests import AgentConversation, text_of_tokens, tool_contents

pytestmark = pytest.mark.subsystem

SUMMARY = "\n".join((
    "## Current Goal", "Read the report.",
    "## Constraints and Decisions", "None.",
    "## Completed Work", "Earlier parts were read.",
    "## Current State", "Reading.",
    "## Relevant Details", "None.",
    "## Next Step", "Continue.",
))


@pytest.fixture(autouse=True)
def thread_db(tmp_path, monkeypatch):
    from row_bot import threads

    monkeypatch.setattr(threads, "DB_PATH", str(tmp_path / "threads.db"))
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path))
    # These conversations live in memory: one checkpoint revision stands for them.
    monkeypatch.setattr(threads, "get_latest_checkpoint_revision", lambda thread_id: "rev-1")
    monkeypatch.setattr(threads, "_latest_checkpoint_revision_conn", lambda conn, thread_id: "rev-1")
    agent._COMPACTION_FAILURES.clear()


@pytest.mark.parametrize(
    ("window", "limit_kind", "usable", "compact_at", "compact_at_before"),
    [
        # Small local windows keep a quarter free, as before: a reply always fits.
        (32_768, "combined", 27_852, 24_575, 24_576),
        (65_536, "combined", 55_705, 49_151, 49_152),
        (131_072, "combined", 111_411, 98_411, 98_304),
        # Larger windows keep 20K for the reply and 13K for the summary, as Claude Code does.
        (200_000, "combined", 180_000, 167_000, 150_000),
        (200_000, "input_only", 190_000, 177_000, 150_000),
        (1_000_000, "input_only", 980_000, 967_000, 750_000),
        (1_000_000, "combined", 980_000, 967_000, 750_000),
    ],
)
def test_compaction_starts_near_the_limit_with_room_for_the_reply_and_the_summary(
    window, limit_kind, usable, compact_at, compact_at_before
) -> None:
    reply, summary = models.context_reserves(window, limit_kind)

    assert window - reply == usable
    assert window - reply - summary == compact_at
    assert compact_at >= compact_at_before - 1  # never earlier than the old 75%
    # Proportionally more is kept free on a small window than on a large one.
    assert (window - compact_at) / window >= 0.033


def test_the_policy_of_a_1m_window_compacts_at_967k(monkeypatch) -> None:
    monkeypatch.setattr(models, "_cloud_context_override", 1_000_000)
    monkeypatch.setattr(models, "get_model_max_context", lambda model_name=None, **_kwargs: None)
    monkeypatch.setattr(models, "_cloud_model_cache", {})
    monkeypatch.setattr(models, "_context_catalog", {})

    policy = models.get_context_policy("model:openai:not-a-maintained-model")

    assert policy.effective_limit_tokens == 1_000_000
    assert policy.usable_input_tokens == 980_000
    assert policy.compact_at_tokens == 967_000


def _summarizer(monkeypatch) -> list:
    calls: list = []

    def summarize(_inputs, *, prior_summary, transcript, output_tokens):
        calls.append(transcript)
        return SUMMARY

    monkeypatch.setattr(agent, "_invoke_compaction_model", summarize)
    return calls


def _cleared(content: str) -> bool:
    return content.startswith("[read_file]: ")


def test_old_tool_output_is_cleared_first_and_summarizing_waits_until_the_request_is_still_over(
    monkeypatch,
) -> None:
    # A large window (so no batch jump comes first) with the compaction point at 20K tokens.
    conversation = AgentConversation(
        agent, monkeypatch, context_window=1_000_000, compact_at=20_000, usable=30_000
    )
    summaries = _summarizer(monkeypatch)
    record = []
    for number in range(1, 30):
        conversation.say(f"Read part {number} of the report.")
        call = conversation.tool("read_file", text_of_tokens(f"part {number}", 2_000))
        record.append((call, conversation.request()))
        # The answers stay: only summarizing can shorten them.
        conversation.answer(text_of_tokens(f"answer {number}", 600))
        if summaries:
            break

    def cleared_calls(request) -> list[str]:
        contents = tool_contents(request)
        return [call for call, _request in record if call in contents and _cleared(contents[call])]

    def summarized(request) -> bool:
        return any("<HISTORICAL_CONTEXT" in str(message.content) for message in request)

    first_clear = next(index for index, (_call, request) in enumerate(record) if cleared_calls(request))
    first_summary = next(index for index, (_call, request) in enumerate(record) if summarized(request))
    # Reaching the compaction point first cleared the old tool output, and that was enough for a while.
    assert first_clear < first_summary
    assert not summarized(record[first_clear][1])
    assert all(not cleared_calls(request) for _call, request in record[:first_clear])
    # Summarizing came later, once clearing could no longer bring the request under the point.
    assert len(summaries) == 1
    assert first_summary == len(record) - 1


def test_the_compaction_point_compares_the_calibrated_estimate(monkeypatch) -> None:
    from tests.subsystem.agents.test_context_compaction import _inputs

    raw = []
    for index in range(4):
        raw.extend((HumanMessage(content=f"user-{index} " + "word " * 400), AIMessage(content=f"answer-{index}")))
    inputs = _inputs(raw)  # compaction point 4,000 tokens, usable 4,533
    monkeypatch.setattr(agent, "_validated_summary_for_inputs", lambda _inputs: None)
    monkeypatch.setattr(agent, "_emit_context_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(agent, "_record_compaction_event", lambda *args, **kwargs: {})
    monkeypatch.setattr("row_bot.threads.save_summary_state_cas", lambda *args, **kwargs: True)
    summaries = _summarizer(monkeypatch)
    token = agent._current_thread_id_var.set("thread-calibrated")
    try:
        plain = agent._prepare_with_compaction(inputs)
        assert plain.usage.estimated_input_tokens == plain.raw_estimated_input_tokens
        assert plain.usage.estimated_input_tokens < inputs.policy.compact_at_tokens
        assert summaries == []

        # The provider counted twice what Row-Bot estimated: the same request now reaches the point.
        context_calibration.observe("openai", "model:openai:test", estimated=10_000, reported=20_000)
        calibrated = agent._prepare_model_input(inputs.raw_messages, inputs, None, mode="agent")
        assert calibrated.usage.estimated_input_tokens == 2 * calibrated.raw_estimated_input_tokens
        assert calibrated.usage.count_source == "calibrated_approximate"
        compacted = agent._prepare_with_compaction(inputs)
    finally:
        agent._current_thread_id_var.reset(token)
    assert len(summaries) == 1
    assert compacted.usage.estimated_input_tokens < inputs.policy.compact_at_tokens


def test_a_summary_brings_a_full_turn_context_snapshot_on_the_next_call(monkeypatch) -> None:
    conversation = AgentConversation(
        agent, monkeypatch, context_window=1_000_000, compact_at=9_000, usable=30_000
    )
    _summarizer(monkeypatch)
    for number in range(1, 6):
        conversation.say(f"Question {number}: " + "detail " * 1_200)
        request = conversation.request()
        conversation.answer(f"Answer {number}.")
        if any("<HISTORICAL_CONTEXT" in str(message.content) for message in request):
            break
    else:  # pragma: no cover - the conversation is sized to compact
        pytest.fail("no compaction")

    from row_bot import turn_context

    contexts = [
        message for message in conversation.state["messages"] if turn_context.is_turn_context_message(message)
    ]
    latest = contexts[-1].additional_kwargs[turn_context.METADATA_KEY]
    assert latest["full"] is True
    assert latest["summary"]  # it names the summary it follows
    assert "Current date and time" in str(request[-1].content)
