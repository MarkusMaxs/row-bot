"""What Agent checkpoints keep, and what a request is built from (WP A4b, F30 and F32).

* F30: the provider request the pre-model hook prepares (``llm_input_messages``, the whole prompt)
  was saved in every checkpoint. It is now never saved: a model call resumed after a failure
  prepares it again, to the same bytes. Each history prefix is hashed once while it is loaded.
* F32: an answer's inline ``<think>`` text was stored, and sent back, as answer text. It is kept as
  the answer's reasoning, where the transcript already shows it.
* An older chart result keeps its Plotly figure in the checkpoint but reaches the model as WP D2's
  summary, the same bytes on every call.
"""

from __future__ import annotations

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.prebuilt import create_react_agent

import row_bot.agent as agent
from row_bot import threads
from row_bot.agent_budget import RowBotAgentState, post_model_budget_hook
from tests.helpers.agent_requests import MODEL_REF, AgentConversation, tool_contents, wire

pytestmark = pytest.mark.subsystem


@pytest.fixture(autouse=True)
def thread_db(tmp_path, monkeypatch):
    monkeypatch.setattr(threads, "DB_PATH", str(tmp_path / "threads.db"))
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path))


class _Recording(FakeMessagesListChatModel):
    """Answers in order, failing its first *failures* calls as an overloaded provider does."""

    failures: int = 0
    seen: list = []

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.seen.append(list(messages))
        if len(self.seen) <= self.failures:
            raise RuntimeError("Our servers are currently overloaded. Please try again later.")
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


def _saved_channels(saver: InMemorySaver, config: dict) -> set[str]:
    channels: set[str] = set()
    for saved in saver.list(config):
        channels.update(saved.checkpoint["channel_values"])
        channels.update(channel for _task, channel, _value in saved.pending_writes or ())
    return channels


def test_the_prepared_request_is_never_saved_in_a_checkpoint() -> None:
    model = _Recording(responses=[AIMessage(content="Hello.", id="ai-1")], seen=[])

    def hook(state):
        return {"llm_input_messages": [SystemMessage(content="THE WHOLE PROMPT"), *state["messages"]]}

    saver = InMemorySaver()
    graph = create_react_agent(model=model, tools=[], state_schema=RowBotAgentState, pre_model_hook=hook,
                               post_model_hook=post_model_budget_hook, checkpointer=saver, version="v2")
    config = {"configurable": {"thread_id": "thread-untracked"}}

    graph.invoke({"messages": [HumanMessage(content="Hi", id="h1")],
                  "execution_budget": agent.new_execution_budget("turn-1")}, config)

    assert model.seen[0][0].content == "THE WHOLE PROMPT"  # it reached the model call
    channels = _saved_channels(saver, config)
    assert "messages" in channels
    assert "llm_input_messages" not in channels
    assert "llm_input_messages" not in graph.get_state(config).values


def test_a_model_call_resumed_after_a_failure_prepares_the_same_request(monkeypatch) -> None:
    conversation = AgentConversation(agent, monkeypatch)
    model = _Recording(responses=[AIMessage(content="Done.", id="ai-1")], failures=1, seen=[])
    graph = create_react_agent(model=agent._AgentModelCall(model, []), tools=[], state_schema=RowBotAgentState,
                               pre_model_hook=agent._pre_model_trim, post_model_hook=post_model_budget_hook,
                               checkpointer=InMemorySaver(), version="v2")
    generation = f"{conversation.thread_id}-turn-1"
    config = {"configurable": {"thread_id": conversation.thread_id, "generation_id": generation}}
    agent._set_active_runtime_context(
        thread_id=conversation.thread_id, runtime_surface="normal_chat", requested_runtime_mode="agent",
        selected_runtime_mode="agent", generation_id=generation, model_override=MODEL_REF,
        enabled_tool_names=("filesystem",),
    )

    with pytest.raises(RuntimeError, match="overloaded"):
        graph.invoke({"messages": [HumanMessage(content="Summarise the notes.", id="h1")],
                      "execution_budget": agent.new_execution_budget(generation)}, config)
    # Resume re-runs only the failed model call: its request was not saved, so it is prepared again.
    graph.invoke(None, config)

    failed, resumed = model.seen
    assert isinstance(resumed[0], SystemMessage)  # the Agent's request, not the bare history
    assert wire(resumed) == wire(failed)
    assert graph.get_state(config).values["messages"][-1].content == "Done."


def test_the_agent_model_call_still_streams_its_tokens_from_the_agent_node() -> None:
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

    model = GenericFakeChatModel(messages=iter([AIMessage(content="Hello there, friend.")]))

    def hook(state):
        return {"llm_input_messages": [SystemMessage(content="system"), *state["messages"]]}

    graph = create_react_agent(model=agent._AgentModelCall(model, []), tools=[], state_schema=RowBotAgentState,
                               pre_model_hook=hook, checkpointer=InMemorySaver(), version="v2")
    config = {"configurable": {"thread_id": "thread-streaming"}}

    chunks = [
        chunk.content
        for chunk, meta in graph.stream({"messages": [HumanMessage(content="Hi", id="h1")],
                                         "execution_budget": agent.new_execution_budget("turn-1")},
                                        config, stream_mode="messages")
        if type(chunk).__name__ == "AIMessageChunk" and meta.get("langgraph_node") == "agent"
    ]

    assert len(chunks) > 1
    assert "".join(chunks) == "Hello there, friend."


def test_each_history_prefix_is_hashed_once_while_it_is_loaded(monkeypatch) -> None:
    calls = []
    original = threads._context_boundary_digest
    monkeypatch.setattr(threads, "_context_boundary_digest", lambda prefix, mode: calls.append(1) or original(prefix, mode))
    messages = [HumanMessage(content=f"message {index}", id=f"m{index}") for index in range(6)]

    first = threads.context_boundary_digest(messages, 4, "agent")
    again = threads.context_boundary_digest(list(messages), 4, "agent")
    assert first == again and len(calls) == 1

    # The same history loaded again (new objects) is hashed again, to the same digest.
    reloaded = [message.model_copy() for message in messages]
    assert threads.context_boundary_digest(reloaded, 4, "agent") == first
    assert len(calls) == 2
    # A changed message is a changed digest.
    edited = [*reloaded[:3], HumanMessage(content="edited", id="m3")]
    assert threads.context_boundary_digest(edited, 4, "agent") != first


def test_inline_thinking_is_stored_as_the_answers_reasoning_and_shown_the_same() -> None:
    from row_bot.message_projection import langchain_messages_to_ui_messages

    stored = AIMessage(content="<think>Check the totals first.</think>The total is 42.", id="ai-1",
                       additional_kwargs={"reasoning_content": "Read the sheet."})

    moved = agent._with_inline_thinking_moved(stored, "openai")

    assert moved.content == "The total is 42."
    assert moved.additional_kwargs["reasoning_content"] == "Read the sheet.\nCheck the totals first."
    question = HumanMessage(content="Total?", id="h1")
    assert langchain_messages_to_ui_messages([question, moved]) == langchain_messages_to_ui_messages([question, stored])
    # A request no longer carries the reasoning as answer text.
    request = agent._normalize_provider_facing_messages([question, moved], provider_id="openai")
    assert "<think>" not in str(request[-1].content)


def test_inline_thinking_stays_for_endpoints_that_replay_reasoning(monkeypatch) -> None:
    monkeypatch.setattr(agent, "_custom_endpoint_supports_reasoning_replay", lambda provider_id: True)
    stored = AIMessage(content="<think>plan</think>Answer.", id="ai-1")

    assert agent._with_inline_thinking_moved(stored, "custom_openai_lab") is stored


def test_the_model_call_stores_inline_thinking_as_reasoning() -> None:
    model = _Recording(responses=[AIMessage(content="<think>plan</think>Answer.", id="ai-1")], seen=[])
    request = [SystemMessage(content="system"), HumanMessage(content="Question?", id="h1")]

    answer = agent._AgentModelCall(model, [])({"llm_input_messages": request, "messages": request}).invoke(
        request, {"configurable": {"thread_id": "thread-think"}}
    )

    assert answer.content == "Answer."
    assert answer.additional_kwargs["reasoning_content"] == "plan"


def test_an_older_chart_result_reaches_the_model_as_its_summary(monkeypatch) -> None:
    import json

    from row_bot import rendered_results

    figure = {"data": [{"type": "bar", "x": ["Q1", "Q2", "Q3"], "y": [3, 5, 8], "name": "Sales"}],
              "layout": {"title": {"text": "Quarterly sales"}}}
    stored = rendered_results.CHART_MARKER + json.dumps(figure) + "\n\nBar chart of quarterly sales."
    conversation = AgentConversation(agent, monkeypatch)
    conversation.say("Chart the quarterly sales.")
    call = conversation.tool("create_chart", stored)

    first = tool_contents(conversation.request())[call]
    second = tool_contents(conversation.request())[call]

    assert first == second  # the same bytes on every call
    assert first.startswith("Bar chart of quarterly sales.\nShown to the person as an interactive bar chart")
    assert '"data"' not in first
    # The checkpoint, which the transcript reads the chart from, keeps the figure.
    kept = next(message for message in conversation.state["messages"] if getattr(message, "tool_call_id", "") == call)
    assert kept.content == stored
