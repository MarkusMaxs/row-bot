"""In an incognito chat the agent saves to memory only when the person says so (N11).

``save_memory`` (and the agent's other memory writes) shows "Save to memory? This
chat is incognito." with the exact text, and only the person answers it: the
Auto reviewer never approves it and Hands-off never skips it. These are
security tests: they stay in the pull request lane.
"""
from __future__ import annotations

import operator
import sqlite3
from typing import Annotated, TypedDict

import pytest

from row_bot import approval_review
from row_bot.approval_review import ReviewVerdict
from row_bot.tools import approval_gate
from tests.contracts.client_platform.test_headless_lifecycle import platform  # noqa: F401 -- a fixture
from tests.fixtures.memory_stack import fresh_memory_stack
from tests.subsystem.client_platform.test_finished_turns_answer_every_call import ProviderShapedModel, _submit
from tests.subsystem.client_platform.test_question_cards import QuestionTurns, _cards, _resolve

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


# ── Several writes in one step (B412) ────────────────────────────────────────
#
# The model saved two memories at once in an incognito chat. Both waited on one generic approval card
# ("The reviewer asked you: …", with Use Auto in this chat), and one Deny skipped both. Each write is its
# own N11 card, one after the other, answered by the person alone. These run the real graph, gate and
# memory tool through the platform's approval path, with a fake model.

WRITES = {"Coffee": "Black, no sugar.", "Tea": "Green, no milk."}
TEXTS = {f"{subject}: {content}" for subject, content in WRITES.items()}


class IncognitoTurns(QuestionTurns):
    """The platform's turns over the real graph; a resume binds the person's answer as the agent does."""

    def resume(self, enabled_tools, config, approved, *, interrupt_ids=None, resume_values=None, stop_event=None):
        from langgraph.types import Command
        from row_bot.agent import _person_answers, _resume_value, _stream_graph

        answer = _person_answers(self.graph.get_state(config), _resume_value(approved, interrupt_ids, resume_values))
        yield from _stream_graph(self.graph, Command(resume=answer), config, stop_event=stop_event)


@pytest.fixture
def private(platform, tmp_path, monkeypatch, reload_for_data_dir):  # noqa: F811
    """The platform's conversation, incognito, with a fresh memory and the real save_memory tool."""
    from langchain_core.messages import AIMessage
    from langchain_core.tools import tool
    from row_bot import threads

    conversations = threads.DB_PATH
    stack = fresh_memory_stack(tmp_path, monkeypatch, reload_for_data_dir)
    monkeypatch.setattr(threads, "DB_PATH", conversations)
    with sqlite3.connect(conversations) as connection:  # Made incognito, as New incognito chat makes it.
        connection.execute("UPDATE thread_meta SET incognito = 1 WHERE thread_id = 'conversation-a'")
    assert threads.incognito_state("conversation-a").incognito
    memory_tool = stack["memory_tool"]

    @tool
    def save_memory(category: str, subject: str, content: str) -> str:
        """Save one memory."""
        with approval_gate.collect_decisions() as decisions:
            result = memory_tool._save_memory(category, subject, content)
        return approval_gate.with_decisions(decisions, result)

    both = AIMessage(content="", id="ai-both", tool_calls=[
        {"name": "save_memory", "args": {"category": "preference", "subject": subject, "content": content},
         "id": f"save-{subject.lower()}", "type": "tool_call"} for subject, content in WRITES.items()])
    model = ProviderShapedModel(responses=[both, AIMessage(content="Saved what you chose.", id="ai-done")])
    return IncognitoTurns(model, [save_memory]), stack


def _saved_subjects(stack) -> set[str]:
    return {subject for subject in WRITES if stack["memory"].find_by_subject("preference", subject) is not None}


def _memory_card(platform, approval_id: str) -> dict:
    card = platform.get_approval(approval_id)
    # The N11 card: its question and exact text; nothing about a reviewer, no Approve the rest.
    assert card["question"] == approval_gate.INCOGNITO_MEMORY_QUESTION
    assert card["memory_text"] in TEXTS
    assert "reviewer_reason" not in card and card["repeatable"] is False
    assert card["scope"] == "Only this requested action will be resolved."
    return card


@pytest.mark.parametrize("mode", ["approve", "auto"])
def test_two_writes_in_one_step_are_two_cards_each_answered_by_itself(platform, private, reviewer, level, mode):  # noqa: F811
    turns, stack = private
    level(mode)
    first = _submit(platform, turns, "two-writes", "Remember how I take coffee and tea")
    assert first.status == "waiting_approval"
    shown = _memory_card(platform, first.approval_id)["memory_text"]

    second = _resolve(platform, first.approval_id, "save-first", {"decision": "approve"})

    # Saved is only what that card showed; the other write asks for itself, on its own card.
    assert second.status == "waiting_approval"
    assert _saved_subjects(stack) == {shown.split(":")[0]}
    other = _memory_card(platform, second.approval_id)["memory_text"]
    assert {shown, other} == TEXTS

    done = _resolve(platform, second.approval_id, "skip-second", {"decision": "reject"})

    assert done.status == "completed"
    assert _saved_subjects(stack) == {shown.split(":")[0]}
    assert [card.get("memory_text") for card in _cards(platform)] == [shown, other]
    assert reviewer == []  # No level and no reviewer answered either one.


def test_dont_save_on_one_write_still_asks_about_the_other(platform, private, level):  # noqa: F811
    turns, stack = private
    level("approve")
    first = _submit(platform, turns, "two-writes", "Remember how I take coffee and tea")
    declined = _memory_card(platform, first.approval_id)["memory_text"]

    second = _resolve(platform, first.approval_id, "skip-first", {"decision": "reject"})

    assert second.status == "waiting_approval"  # One Don't save never skips the other write.
    assert _saved_subjects(stack) == set()
    kept = _memory_card(platform, second.approval_id)["memory_text"]
    assert kept != declined
    done = _resolve(platform, second.approval_id, "save-second", {"decision": "approve"})
    assert done.status == "completed"
    assert _saved_subjects(stack) == {kept.split(":")[0]}
