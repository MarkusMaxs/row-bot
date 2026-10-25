"""A turn's own checkpoint does not stop the compaction of its overflow retry (F27).

A real ``stream_agent`` turn over a long saved conversation: the provider refuses the first model
call as too long, so Row-Bot compacts the conversation and asks again. LangGraph writes each step's
checkpoint in a background thread while the next step runs (its default "async" durability), so the
checkpoint of the turn's pre-model step can land while that compaction runs. Here the step's write
is held until the summarizer is asked and lands before the summary is saved: the order seen under
parallel load. A real change to the conversation in that window still stops the compaction.
"""

from __future__ import annotations

import threading
import uuid
from typing import Any

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage

import row_bot.agent as agent
from row_bot import threads
from tests.helpers.agent_requests import MODEL_REF, AgentConversation, text_of_tokens

pytestmark = pytest.mark.subsystem

SUMMARY = "\n".join((
    "## Current Goal", "Sum up the quarterly figures.",
    "## Constraints and Decisions", "None.",
    "## Completed Work", "Questions 1 to 4 were answered.",
    "## Current State", "Answering.",
    "## Relevant Details", "None.",
    "## Next Step", "Answer the question.",
))
ANSWER = "The figures add up."
# A safety net against a deadlock if the code under test goes another way: never reached otherwise.
WAIT_SECONDS = 30


class RefusesFirstRequest(FakeMessagesListChatModel):
    """The provider: refuses the first request as too long, then answers. Keeps every request."""

    seen: list = []

    def bind_tools(self, tools: Any, **kwargs: Any) -> RefusesFirstRequest:
        return self

    def _generate(self, messages: Any, stop: Any = None, run_manager: Any = None, **kwargs: Any) -> Any:
        self.seen.append(list(messages))
        if len(self.seen) == 1:
            raise ValueError("Error code: 400 - prompt is too long: 250000 tokens > 200000 maximum")
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


class HeldStepCheckpoints:
    """The graph's step checkpoints of one thread, held in LangGraph's background writer until released."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, thread_id: str) -> None:
        self.released = threading.Event()
        self.landed = threading.Condition()
        self.sizes: list[int] = []  # how many messages each landed step checkpoint holds
        put = threads.checkpointer.put

        def held_put(config: dict, checkpoint: Any, metadata: Any, new_versions: Any) -> Any:
            step = (config.get("configurable") or {}).get("thread_id") == thread_id and (
                (metadata or {}).get("source") == "loop")
            if step:
                assert self.released.wait(WAIT_SECONDS), "the held checkpoints were never released"
            written = put(config, checkpoint, metadata, new_versions)
            if step:
                with self.landed:
                    self.sizes.append(len(checkpoint["channel_values"].get("messages") or []))
                    self.landed.notify_all()
            return written

        monkeypatch.setattr(threads.checkpointer, "put", held_put)

    def land(self, messages: int) -> None:
        """Let the held writes land, and wait until the one holding *messages* messages has."""
        self.released.set()
        with self.landed:
            assert self.landed.wait_for(lambda: messages in self.sizes, WAIT_SECONDS), self.sizes


class Turn:
    """One saved conversation of six long questions, and a new turn on it through ``stream_agent``."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from row_bot.agent_budget import RowBotAgentState, post_model_budget_hook

        # Under the compaction point (so the pre-model step doesn't compact), but the provider refuses it.
        self.conversation = AgentConversation(agent, monkeypatch, compact_at=30_000, usable=40_000)
        self.thread_id = self.conversation.thread_id
        history: list = []
        for number in range(1, 7):
            history += [
                HumanMessage(content=f"Question {number}: " + text_of_tokens(f"q{number}", 2_000), id=f"q{number}"),
                AIMessage(content=f"Answer {number}.", id=f"a{number}"),
            ]
        assert threads.append_checkpoint_messages(self.thread_id, history)
        self.model = RefusesFirstRequest(responses=[AIMessage(content=ANSWER)], seen=[])
        graph = agent.create_react_agent(
            model=agent._AgentModelCall(self.model, []), tools=[], prompt=None,
            pre_model_hook=agent._pre_model_trim, post_model_hook=post_model_budget_hook,
            checkpointer=threads.checkpointer, name="row_bot_agent", state_schema=RowBotAgentState, version="v2",
        )

        def graph_for_this_turn(*_args: Any, **_kwargs: Any) -> Any:
            agent._current_canonical_tools_var.set(())
            return graph

        monkeypatch.setattr(agent, "get_agent_graph", graph_for_this_turn)
        self.held = HeldStepCheckpoints(monkeypatch, self.thread_id)
        self.summaries: list[str] = []
        self.meanwhile = lambda: None

        def summarize(inputs: Any, *, prior_summary: str, transcript: str, output_tokens: int) -> str:
            self.summaries.append(transcript)
            if len(self.summaries) == 1:
                # The compaction has prepared (and read the checkpoint revision). Now the turn's step
                # checkpoints land, up to the pre-model step's: the conversation as the turn holds it.
                self.held.land(len(inputs.raw_messages))
                self.meanwhile()
            return SUMMARY

        monkeypatch.setattr(agent, "_invoke_compaction_model", summarize)

    def send(self, text: str) -> list[tuple[str, Any]]:
        config = {"configurable": {
            "thread_id": self.thread_id,
            "generation_id": f"{self.thread_id}-turn-1",
            "runtime_surface": "normal_chat",
            "runtime_mode": "agent",
            "approval_mode": "approve",
            "model_override": MODEL_REF,
        }}
        return list(agent.stream_agent(text, [], config))

    def saved_summary(self) -> dict | None:
        return threads.load_validated_summary_state(
            self.thread_id, "agent", messages=threads.get_latest_checkpoint_messages(self.thread_id))


@pytest.fixture(autouse=True)
def _no_remembered_failures():
    agent._COMPACTION_FAILURES.clear()
    yield
    agent._COMPACTION_FAILURES.clear()


def _summarized(request: list) -> bool:
    return any("<HISTORICAL_CONTEXT" in str(message.content) for message in request)


def test_the_turns_own_checkpoint_landing_mid_compaction_does_not_stop_the_retry(monkeypatch) -> None:
    turn = Turn(monkeypatch)

    events = turn.send("Sum up the figures.")

    assert [kind for kind, _payload in events if kind in {"error", "compaction_failed"}] == []
    assert events[-1] == ("done", ANSWER)
    assert [kind for kind, _payload in events].count("compaction_succeeded") == 1
    # Refused once, then asked again with the older questions summarized: a shorter request.
    refused, retried = turn.model.seen
    assert not _summarized(refused)
    assert _summarized(retried)
    assert len(retried) < len(refused)
    # The summary is kept for the conversation the turn left.
    saved = turn.saved_summary()
    assert saved is not None and saved["summary"] == SUMMARY


@pytest.mark.parametrize("change", ["another message arrives", "an earlier question is edited"])
def test_a_real_change_to_the_conversation_mid_compaction_still_stops_it(monkeypatch, change) -> None:
    turn = Turn(monkeypatch)

    def change_the_conversation() -> None:
        if change == "another message arrives":
            assert threads.append_checkpoint_messages(
                turn.thread_id, [HumanMessage(content="A message from another device.", id="elsewhere")])
        else:
            threads.replace_admitted_human_content(
                turn.thread_id, "q2", "Question 2, edited.",
                expected_revision=threads.get_latest_checkpoint_revision(turn.thread_id))

    turn.meanwhile = change_the_conversation

    events = turn.send("Sum up the figures.")

    # As before: the compaction stops, nothing is saved, and the provider's refusal ends the turn.
    failures = [payload for kind, payload in events if kind == "compaction_failed"]
    assert [payload["reason"] for payload in failures] == ["Conversation changed while compaction was finishing."]
    assert events[-1][0] == "error"
    assert "done" not in [kind for kind, _payload in events]
    assert len(turn.model.seen) == 1
    assert turn.saved_summary() is None


@pytest.mark.parametrize("since", ["the turn's own message landed", "the last answer was removed"])
def test_a_newer_checkpoint_is_the_same_conversation_only_when_it_grew_toward_the_turn(since) -> None:
    thread_id = f"own-checkpoint-{uuid.uuid4().hex[:10]}"
    threads.create_thread("Own checkpoint", thread_id=thread_id, seed_default_skills=False)
    history = [HumanMessage(content="q1", id="q1"), AIMessage(content="a1", id="a1"),
               HumanMessage(content="q2", id="q2"), AIMessage(content="a2", id="a2")]
    assert threads.append_checkpoint_messages(thread_id, history)
    prepared_at = threads.get_latest_checkpoint_revision(thread_id)
    holds = [*history, HumanMessage(content="q3", id="q3")]
    state = {
        "schema_version": threads.SUMMARY_STATE_SCHEMA_VERSION, "mode": "agent", "source_revision": prepared_at,
        "boundary_message_count": 2, "boundary_digest": threads.context_boundary_digest(holds, 2, "agent"),
        "summary": SUMMARY,
    }
    if since == "the turn's own message landed":
        assert threads.append_checkpoint_messages(thread_id, holds[-1:])
    else:
        # What is left is still the start of what the turn holds, but the turn didn't remove it.
        assert threads.remove_latest_checkpoint_ai_message(thread_id, "a2")

    saved = threads.save_summary_state_cas(thread_id, state, expected_revision=prepared_at, messages=holds)

    newest = threads.get_latest_checkpoint_revision(thread_id)
    loaded = threads.load_validated_summary_state(
        thread_id, "agent", messages=threads.get_latest_checkpoint_messages(thread_id))
    if since == "the turn's own message landed":
        assert saved and newest != prepared_at
        assert loaded is not None and loaded["source_revision"] == newest
    else:
        assert not saved and loaded is None
