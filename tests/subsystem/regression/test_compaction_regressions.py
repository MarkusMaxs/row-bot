"""Compaction bugs fixed in WP A4b (findings F25-F28) and thinking at compaction.

* F25: the summary splice counted raw messages but sliced prepared ones, so a dropped empty or
  reasoning-only turn before the boundary cut the person's message or orphaned a tool result.
* F26: a failed compaction was remembered per checkpoint revision, which changes on every tool step,
  so it ran again (with its summarizer calls) on every model call.
* F27: a provider's context-overflow error was never retried, so a thread could fail every turn.
* F28: Agent and Chat Only shared one summary slot, so switching modes discarded it.
* Retained turns' thinking blocks are bound to the history the summary replaced: they leave the
  request for completed turns, never for a tool round in progress.
"""

from __future__ import annotations

import contextvars
from dataclasses import replace

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph.message import add_messages

import row_bot.agent as agent
from row_bot import history_hygiene, threads, turn_context
from tests.helpers.agent_requests import MODEL_REF, AgentConversation
from tests.subsystem.agents.test_context_compaction import _inputs

pytestmark = pytest.mark.subsystem

SUMMARY = "\n".join((
    "## Current Goal", "Write the report.",
    "## Constraints and Decisions", "None.",
    "## Completed Work", "Earlier questions were answered.",
    "## Current State", "Working.",
    "## Relevant Details", "None.",
    "## Next Step", "Continue.",
))


@pytest.fixture(autouse=True)
def thread_db(tmp_path, monkeypatch):
    monkeypatch.setattr(threads, "DB_PATH", str(tmp_path / "threads.db"))
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path))
    # These conversations live in memory: one checkpoint revision stands for them.
    monkeypatch.setattr(threads, "get_latest_checkpoint_revision", lambda thread_id: "rev-1")
    monkeypatch.setattr(threads, "_latest_checkpoint_revision_conn", lambda conn, thread_id: "rev-1")
    agent._COMPACTION_FAILURES.clear()
    yield
    agent._COMPACTION_FAILURES.clear()


def _summarizer(monkeypatch, *, fail: bool = False) -> list:
    calls: list = []

    def summarize(_inputs, *, prior_summary, transcript, output_tokens):
        calls.append(transcript)
        if fail:
            raise RuntimeError("summarizer unavailable")
        return SUMMARY

    monkeypatch.setattr(agent, "_invoke_compaction_model", summarize)
    return calls


def _history_ids(messages: list) -> list[str]:
    return [str(message.id) for message in messages if not isinstance(message, SystemMessage) and message.id]


# ── F25: splice by identity ──────────────────────────────────────────────────────────────────────

def test_the_summary_splice_keeps_the_kept_turn_whole_after_dropped_turns_before_it() -> None:
    raw = [
        HumanMessage(content="Start the report.", id="h1"),
        AIMessage(content="", additional_kwargs={"reasoning_content": "Only thinking."}, id="a1"),
        AIMessage(content="", id="a2"),
        AIMessage(content="Started.", id="a3"),
        HumanMessage(content="Add the totals.", id="h2"),
        AIMessage(content="", tool_calls=[{"id": "c1", "name": "calc", "args": {}, "type": "tool_call"}], id="a4"),
        ToolMessage(content="42", name="calc", tool_call_id="c1", id="t1"),
        AIMessage(content="Totals added.", id="a5"),
        HumanMessage(content="Now the summary.", id="h3"),
    ]
    # Preparation drops the reasoning-only and the empty turn before the boundary.
    prepared = agent._normalize_provider_facing_messages([SystemMessage(content="system"), *raw], provider_id="openai")
    assert _history_ids(prepared) == ["h1", "a3", "h2", "a4", "t1", "a5", "h3"]
    inputs = replace(_inputs(raw), complete_messages=tuple(prepared))
    summary = {"schema_version": 1, "mode": "agent", "boundary_message_count": 4, "summary": SUMMARY}

    spliced = agent._messages_with_summary(inputs, summary)

    # The summary covers h1-a3; the person's message h2 and its whole tool round stay.
    assert _history_ids(spliced) == ["h2", "a4", "t1", "a5", "h3"]
    assert "<HISTORICAL_CONTEXT" in str(spliced[spliced.index(next(m for m in spliced if m.id == "h2")) - 1].content)


# ── F26: no retry storm ──────────────────────────────────────────────────────────────────────────

def _over_the_point(groups: int = 4) -> list:
    raw = []
    for index in range(groups):
        raw.extend((
            HumanMessage(content=f"question {index} " + "word " * 800, id=f"h{index}"),
            AIMessage(content=f"answer {index}", id=f"a{index}"),
        ))
    return raw


def _step(raw: list, *, turn: str, revision: str) -> agent.PreparationInputs:
    return replace(_inputs(raw), checkpoint_revision=revision, execution_budget={"budget_id": turn})


def test_a_failing_compaction_is_attempted_once_per_turn_not_on_every_call(monkeypatch) -> None:
    threads.create_thread("Failing compaction", thread_id="thread-failing", seed_default_skills=False)
    attempts = _summarizer(monkeypatch, fail=True)
    monkeypatch.setattr(agent, "_emit_context_event", lambda *args, **kwargs: None)
    raw = _over_the_point()
    token = agent._current_thread_id_var.set("thread-failing")
    try:
        # One turn, five tool steps: the checkpoint revision changes on every one.
        statuses = [
            agent._prepare_with_compaction(_step(raw, turn="turn-1", revision=f"rev-{step}")).usage.status
            for step in range(5)
        ]
        assert len(attempts) == 1
        assert statuses == ["failed"] * 5  # the request still fits: the turn goes on, uncompacted

        # The next turn gets one more attempt, and only one.
        agent._prepare_with_compaction(_step(raw, turn="turn-2", revision="rev-9"))
        agent._prepare_with_compaction(_step(raw, turn="turn-2", revision="rev-10"))
        assert len(attempts) == 2

        # The marker is kept with the thread, not only in this process.
        agent._COMPACTION_FAILURES.clear()
        assert threads.load_compaction_failure("thread-failing", "agent")["turn"] == "turn-2"
        agent._prepare_with_compaction(_step(raw, turn="turn-2", revision="rev-11"))
        assert len(attempts) == 2

        # New input to age (one more complete group) is a new attempt, even in the same turn.
        grown = [*raw, HumanMessage(content="one more " + "word " * 10, id="h-more")]
        agent._prepare_with_compaction(_step(grown, turn="turn-2", revision="rev-12"))
        assert len(attempts) == 3
    finally:
        agent._current_thread_id_var.reset(token)


def test_a_compaction_that_succeeds_clears_the_failure_marker(monkeypatch) -> None:
    threads.create_thread("Recovering compaction", thread_id="thread-recovering", seed_default_skills=False)
    monkeypatch.setattr(agent, "_emit_context_event", lambda *args, **kwargs: None)
    raw = _over_the_point()
    token = agent._current_thread_id_var.set("thread-recovering")
    try:
        _summarizer(monkeypatch, fail=True)
        agent._prepare_with_compaction(_step(raw, turn="turn-1", revision="rev-1"))
        assert threads.load_compaction_failure("thread-recovering", "agent")
        _summarizer(monkeypatch)
        prepared = agent._prepare_with_compaction(_step(raw, turn="turn-2", revision="rev-1"))
    finally:
        agent._current_thread_id_var.reset(token)
    assert prepared.usage.status == "ready"
    assert threads.load_compaction_failure("thread-recovering", "agent") is None
    assert ("thread-recovering", "agent") not in agent._COMPACTION_FAILURES


# ── F27: reactive overflow ───────────────────────────────────────────────────────────────────────

class _Overflowing(FakeMessagesListChatModel):
    """Refuses the first *overflows* requests as too long, then answers; records every request."""

    overflows: int = 1
    seen: list = []

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.seen.append(list(messages))
        if len(self.seen) <= self.overflows:
            raise ValueError("Error code: 400 - prompt is too long: 250000 tokens > 200000 maximum")
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


def _long_conversation(monkeypatch) -> AgentConversation:
    conversation = AgentConversation(agent, monkeypatch, context_window=200_000)
    for number in range(1, 5):
        conversation.say(f"Question {number}: " + "detail " * 800)
        conversation.request()
        conversation.answer(f"Answer {number}.")
    conversation.say("Question 5: and the totals?")
    return conversation


def _model_call(conversation: AgentConversation, model) -> AIMessage:
    """One Agent model call as the graph makes it: the pre-model hook, then the model."""

    def run() -> AIMessage:
        generation = f"{conversation.thread_id}-turn-{conversation.turns}"
        agent._set_active_runtime_context(
            thread_id=conversation.thread_id, runtime_surface="normal_chat", requested_runtime_mode="agent",
            selected_runtime_mode="agent", generation_id=generation, model_override=MODEL_REF,
            enabled_tool_names=("filesystem",),
        )
        update = agent._pre_model_trim(conversation.state)
        conversation.state["messages"] = add_messages(conversation.state["messages"], update.get("messages") or [])
        request = update["llm_input_messages"]
        # What create_react_agent hands the model: the prepared request under "messages".
        node_state = {**conversation.state, "llm_input_messages": request, "messages": request}
        config = {"configurable": {"thread_id": conversation.thread_id, "generation_id": generation}}
        return agent._AgentModelCall(model, [])(node_state).invoke(request, config)

    return contextvars.copy_context().run(run)


def _summarized(request: list) -> bool:
    return any("<HISTORICAL_CONTEXT" in str(message.content) for message in request)


def test_a_provider_overflow_compacts_once_and_retries_once(monkeypatch) -> None:
    conversation = _long_conversation(monkeypatch)
    summaries = _summarizer(monkeypatch)
    model = _Overflowing(responses=[AIMessage(content="The totals are 42.", id="ai-answer")], seen=[])

    answer = _model_call(conversation, model)

    assert answer.content == "The totals are 42."
    first, retry = model.seen
    assert len(summaries) == 1
    assert not _summarized(first) and _summarized(retry)
    assert agent._count_prepared_tokens(retry, ()) < agent._count_prepared_tokens(first, ())
    # The person's question is still the request's last word.
    assert "Question 5: and the totals?" in str(retry[-1].content)

    # The next call of the turn persists a full turn-context snapshot after the summary.
    conversation.state["messages"] = add_messages(conversation.state["messages"], [answer])
    conversation.request()
    contexts = [m for m in conversation.state["messages"] if turn_context.is_turn_context_message(m)]
    assert contexts[-1].additional_kwargs[turn_context.METADATA_KEY]["full"] is True


def test_a_second_overflow_stands(monkeypatch) -> None:
    conversation = _long_conversation(monkeypatch)
    summaries = _summarizer(monkeypatch)
    model = _Overflowing(responses=[AIMessage(content="unused")], overflows=5, seen=[])

    with pytest.raises(ValueError, match="prompt is too long"):
        _model_call(conversation, model)

    assert len(model.seen) == 2  # the request and one compacted retry, no more
    assert len(summaries) == 1


def test_an_overflow_with_nothing_to_compact_is_not_retried(monkeypatch) -> None:
    conversation = AgentConversation(agent, monkeypatch, context_window=200_000)
    conversation.say("A single long question: " + "detail " * 800)
    summaries = _summarizer(monkeypatch)
    model = _Overflowing(responses=[AIMessage(content="unused")], seen=[])

    with pytest.raises(ValueError, match="prompt is too long"):
        _model_call(conversation, model)

    assert len(model.seen) == 1 and summaries == []


def test_chat_only_compacts_and_asks_again_after_an_overflow(monkeypatch) -> None:
    raw = []
    for index in range(4):
        raw.extend((HumanMessage(content=f"question {index} " + "word " * 300), AIMessage(content=f"answer {index}")))
    inputs = _inputs(raw, mode="chat_only")
    summaries = _summarizer(monkeypatch)
    monkeypatch.setattr(agent, "_emit_context_event", lambda *args, **kwargs: None)
    monkeypatch.setattr("row_bot.threads.save_summary_state_cas", lambda *args, **kwargs: True)
    token = agent._current_thread_id_var.set("thread-chat-only")
    try:
        prepared = agent._prepare_with_compaction(inputs)
        assert prepared.usage.estimated_input_tokens < inputs.policy.compact_at_tokens
        retry = agent._chat_only_request_after_overflow(
            ValueError("context_length_exceeded"), inputs, prepared
        )
    finally:
        agent._current_thread_id_var.reset(token)

    assert retry is not None
    retried, messages = retry
    assert len(summaries) == 1
    assert _summarized(messages)
    assert retried.raw_estimated_input_tokens < prepared.raw_estimated_input_tokens


# ── F28: a summary slot per mode ─────────────────────────────────────────────────────────────────

def test_agent_and_chat_only_keep_their_own_summaries() -> None:
    threads.create_thread("Two modes", thread_id="thread-modes", seed_default_skills=False)
    messages = [HumanMessage(content="first", id="h1"), AIMessage(content="one", id="a1"),
                HumanMessage(content="second", id="h2"), AIMessage(content="two", id="a2")]

    def state(mode: str, text: str) -> dict:
        return {
            "schema_version": 1, "mode": mode, "boundary_message_count": 2, "summary": SUMMARY + text,
            "source_revision": "", "boundary_digest": threads.context_boundary_digest(messages, 2, mode),
        }

    assert threads.save_summary_state_cas("thread-modes", state("agent", " agent"), expected_revision="rev-1")
    assert threads.save_summary_state_cas("thread-modes", state("chat_only", " chat"), expected_revision="rev-1")

    agent_state = threads.load_validated_summary_state("thread-modes", "agent", messages=messages)
    chat_state = threads.load_validated_summary_state("thread-modes", "chat_only", messages=messages)
    assert agent_state["summary"].endswith(" agent")
    assert chat_state["summary"].endswith(" chat")


def test_a_summary_saved_before_the_slots_still_serves_its_mode() -> None:
    import json
    import sqlite3
    from contextlib import closing

    threads.create_thread("Older row", thread_id="thread-older", seed_default_skills=False)
    messages = [HumanMessage(content="first", id="h1"), AIMessage(content="one", id="a1")]
    legacy = {
        "schema_version": 1, "mode": "chat_only", "boundary_message_count": 1, "summary": SUMMARY,
        "boundary_digest": threads.context_boundary_digest(messages, 1, "chat_only"),
    }
    with closing(sqlite3.connect(threads.DB_PATH)) as connection, connection:
        connection.execute("UPDATE thread_meta SET summary_state_json = ? WHERE thread_id = ?",
                           (json.dumps(legacy), "thread-older"))

    assert threads.load_validated_summary_state("thread-older", "chat_only", messages=messages)
    assert threads.load_validated_summary_state("thread-older", "agent", messages=messages) is None


# ── Thinking at compaction ───────────────────────────────────────────────────────────────────────

def _thinking(text: str) -> dict:
    return {"type": "thinking", "thinking": text, "signature": "sig-" + text}


def _thinking_history() -> list:
    return [
        HumanMessage(content="Plan the trip.", id="h1"),
        AIMessage(content=[_thinking("t1"), {"type": "text", "text": "Planned."}], id="a1"),
        HumanMessage(content="Book the train.", id="h2"),
        AIMessage(content=[_thinking("t2"), {"type": "text", "text": "Booked."}], id="a2"),
        HumanMessage(content="Now the hotel.", id="h3"),
        AIMessage(
            content=[_thinking("t3"), {"type": "tool_use", "id": "c3", "name": "search", "input": {}}],
            tool_calls=[{"id": "c3", "name": "search", "args": {}, "type": "tool_call"}],
            id="a3",
        ),
        ToolMessage(content="Three hotels.", name="search", tool_call_id="c3", id="r3"),
    ]


def _thinking_of(messages: list, message_id: str) -> list[str]:
    message = next(m for m in messages if m.id == message_id)
    return [block["thinking"] for block in message.content if isinstance(block, dict) and block.get("type") == "thinking"]


def test_compaction_strips_thinking_from_completed_kept_turns_but_not_from_a_tool_round(monkeypatch) -> None:
    raw = _thinking_history()
    inputs = replace(_inputs(raw), provider_id="anthropic", model_ref="model:anthropic:claude-opus-5-5")
    saved = []
    monkeypatch.setattr(agent, "_emit_context_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(agent, "_record_compaction_event", lambda *args, **kwargs: {})
    monkeypatch.setattr("row_bot.threads.save_summary_state_cas",
                        lambda thread_id, state, expected_revision: saved.append(dict(state)) or True)
    monkeypatch.setattr(agent, "_choose_compaction_boundary", lambda inputs, previous: 2)
    _summarizer(monkeypatch)
    token = agent._current_thread_id_var.set("thread-thinking")
    try:
        agent._prepare_with_compaction(inputs, force=True)
    finally:
        agent._current_thread_id_var.reset(token)

    # The summary marks where the turns complete at compaction end: the turn in progress (h3) starts.
    state = saved[0]
    assert history_hygiene.resolve(raw, state["thinking_strip"]) == 4

    request = agent._messages_with_summary(inputs, state)
    assert _thinking_of(request, "a2") == []  # completed, kept: its block no longer verifies
    assert [block["text"] for block in next(m for m in request if m.id == "a2").content] == ["Booked."]
    assert _thinking_of(request, "a3") == ["t3"]  # the tool round in progress keeps its thinking
    # The checkpoint keeps every block.
    assert _thinking_of(raw, "a2") == ["t2"]


def test_thinking_is_never_stripped_inside_the_turn_in_progress() -> None:
    raw = _thinking_history()
    inputs = replace(_inputs(raw), provider_id="anthropic", model_ref="model:anthropic:claude-opus-5-5")
    # A summary whose mark reaches into the turn in progress (a stale or rewound one).
    state = {
        "schema_version": 1, "mode": "agent", "boundary_message_count": 2, "summary": SUMMARY,
        "thinking_strip": history_hygiene.anchor(raw, 6),
    }

    request = agent._messages_with_summary(inputs, state)

    assert _thinking_of(request, "a2") == []
    assert _thinking_of(request, "a3") == ["t3"]
