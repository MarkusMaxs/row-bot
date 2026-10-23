"""In an incognito chat the agent saves to memory only when the person says so (N11).

``save_memory`` (and the agent's other memory writes) shows "Save to memory? This
chat is incognito." with the exact text, and only the person answers it: the
Auto reviewer never approves it and Hands-off never skips it. These are
security tests: they stay in the pull request lane.
"""
from __future__ import annotations

import operator
from typing import Annotated, TypedDict

import pytest

from row_bot import approval_review
from row_bot.approval_review import ReviewVerdict
from row_bot.tools import approval_gate
from tests.fixtures.memory_stack import fresh_memory_stack

pytestmark = pytest.mark.subsystem


@pytest.fixture
def stack(tmp_path, monkeypatch, reload_for_data_dir):
    stack = fresh_memory_stack(tmp_path, monkeypatch, reload_for_data_dir)
    from row_bot import threads

    threads.create_thread("Private", thread_id="private", seed_default_skills=False, incognito=True)
    threads.create_thread("Everyday", thread_id="everyday", seed_default_skills=False)
    return stack


@pytest.fixture
def reviewer(monkeypatch):
    """A reviewer that would approve anything, and every action it was shown."""
    seen: list[dict] = []

    def review(payload, *, risks=(), **_kwargs):
        seen.append(dict(payload))
        return ReviewVerdict("approve", "Looks like what you asked for.")

    monkeypatch.setattr(approval_review, "review", review)
    return seen


@pytest.fixture
def level(monkeypatch):
    def set_level(mode: str) -> None:
        monkeypatch.setattr(approval_gate, "current_approval_mode", lambda: mode)
    return set_level


def _graph(memory_tool):
    """One tool call, as the agent makes it in a conversation: save_memory, in a graph run."""
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.graph import END, START, StateGraph

    class State(TypedDict):
        results: Annotated[list, operator.add]

    def node(_state):
        # As the agent's tool wrapper runs it: the approval decided leads the result.
        with approval_gate.collect_decisions() as decisions:
            result = memory_tool._save_memory("preference", "Coffee", "I take it black, no sugar.")
        return {"results": [approval_gate.with_decisions(decisions, result)]}

    builder = StateGraph(State)
    builder.add_node("act", node)
    builder.add_edge(START, "act")
    builder.add_edge("act", END)
    return builder.compile(checkpointer=InMemorySaver())


def _saved(stack) -> bool:
    return stack["memory"].find_by_subject("preference", "Coffee") is not None


@pytest.mark.parametrize("mode", ["approve", "auto", "full"])
def test_an_incognito_chat_asks_the_person_under_every_level_and_saves_on_save(stack, reviewer, level, mode):
    from langgraph.types import Command
    from row_bot.agent import _person_answers

    level(mode)
    graph = _graph(stack["memory_tool"])
    config = {"configurable": {"thread_id": "private"}}

    paused = graph.invoke({"results": []}, config)

    [card] = paused["__interrupt__"]
    assert card.value["person_only"] is True
    assert card.value["memory_save"] == {"text": "Coffee: I take it black, no sugar."}
    assert reviewer == [] and not _saved(stack)  # The reviewer never decides it; Hands-off never skips it.

    # The person taps Save on the card in their conversation.
    answer = _person_answers(graph.get_state(config), {card.id: True})
    final = graph.invoke(Command(resume=answer), config)

    assert final["results"][0].startswith(approval_gate.APPROVAL_GIVEN)
    assert "Memory saved" in final["results"][0]
    assert _saved(stack)
    assert reviewer == []


@pytest.mark.parametrize("answer", [False, True])
def test_dont_save_or_an_answer_not_from_the_person_saves_nothing(stack, reviewer, level, answer):
    """Don't save; and a plain "yes" (what a level gives) never stands in for the person."""
    from langgraph.types import Command

    level("full")
    graph = _graph(stack["memory_tool"])
    config = {"configurable": {"thread_id": "private"}}
    graph.invoke({"results": []}, config)

    final = graph.invoke(Command(resume=answer), config)

    assert final["results"][0].startswith(approval_gate.APPROVAL_DENIED)
    assert "Not saved" in final["results"][0]
    assert not _saved(stack)


def test_an_everyday_chat_saves_without_a_card(stack, level):
    level("approve")
    graph = _graph(stack["memory_tool"])

    final = graph.invoke({"results": []}, {"configurable": {"thread_id": "everyday"}})

    assert "__interrupt__" not in final
    assert "Memory saved" in final["results"][0]
    assert _saved(stack)


def test_the_card_shows_the_question_and_the_exact_text_without_a_reviewer_note(stack, level):
    from row_bot.application.approval_projection import project_approval_context

    level("auto")
    graph = _graph(stack["memory_tool"])
    config = {"configurable": {"thread_id": "private"}}
    [card] = graph.invoke({"results": []}, config)["__interrupt__"]

    view = project_approval_context({**card.value, "__interrupt_id": card.id})

    assert view["question"] == "Save to memory? This chat is incognito."
    assert view["memory_text"] == "Coffee: I take it black, no sugar."
    assert "reviewer_reason" not in view
