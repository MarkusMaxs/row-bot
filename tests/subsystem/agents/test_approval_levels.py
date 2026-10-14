"""Who gives an approval at each level (A0): the person under Ask, the reviewer first under Auto, Full autonomy
itself under Full; Block refuses. Every decision is recorded on the action's result."""

from __future__ import annotations

import operator
from typing import Annotated, TypedDict

import pytest

from row_bot import approval_review
from row_bot.approval_review import ReviewVerdict
from row_bot.tools import approval_gate

pytestmark = pytest.mark.subsystem

_PAYLOAD = {"tool": "send_gmail_message", "label": "Send email", "description": "Send email: to pat@example.com"}


@pytest.fixture
def reviewer(monkeypatch):
    """A stand-in reviewer: its decision, and every action it was shown."""
    state = {"verdict": ReviewVerdict("approve", "You asked to email Pat."), "seen": []}

    def review(payload, *, risks=(), **_kwargs):
        state["seen"].append((dict(payload), tuple(risks)))
        return state["verdict"]

    monkeypatch.setattr(approval_review, "review", review)
    return state


def _person(answer):
    asked: list[dict] = []

    def callback(payload):
        asked.append(payload)
        return answer
    return asked, callback


def test_block_refuses_without_asking_anyone(reviewer):
    asked, callback = _person(True)
    outcome, line = approval_gate.resolve_approval_line(dict(_PAYLOAD), approval_mode="block",
                                                        approval_callback=callback)
    assert (outcome, line) == ("block", "")
    assert asked == [] and reviewer["seen"] == []


@pytest.mark.parametrize(("answer", "outcome", "line"), [
    (True, "allow", "Approval: asked; approved by you"),
    (False, "deny", "Approval: asked; denied by you — did not run"),
])
def test_ask_asks_the_person_and_never_the_reviewer(reviewer, answer, outcome, line):
    asked, callback = _person(answer)
    assert approval_gate.resolve_approval_line(dict(_PAYLOAD), approval_mode="approve",
                                               approval_callback=callback) == (outcome, line)
    assert [payload["label"] for payload in asked] == ["Send email"]
    assert "review" not in asked[0]
    assert reviewer["seen"] == []


def test_auto_runs_what_the_reviewer_approves_without_asking_and_records_why(reviewer):
    asked, callback = _person(False)
    outcome, line = approval_gate.resolve_approval_line(dict(_PAYLOAD), approval_mode="auto",
                                                        risks=("reaches other people",), approval_callback=callback)
    assert outcome == "allow"
    assert line == "Approval: approved by the reviewer: You asked to email Pat"
    assert asked == []
    [(seen, risks)] = reviewer["seen"]
    assert seen["description"] == _PAYLOAD["description"] and risks == ("reaches other people",)


@pytest.mark.parametrize(("answer", "outcome", "first"), [
    (True, "allow", "Approval: asked; approved by you"),
    (False, "deny", "Approval: asked; denied by you — did not run"),
])
def test_auto_asks_the_person_when_the_reviewer_asks_and_says_why(reviewer, answer, outcome, first):
    reviewer["verdict"] = ReviewVerdict("ask", "You didn't ask to email anyone.")
    asked, callback = _person(answer)
    result, line = approval_gate.resolve_approval_line(dict(_PAYLOAD), approval_mode="auto",
                                                       approval_callback=callback)
    assert result == outcome
    assert line == f"{first}. The reviewer asked you: You didn't ask to email anyone"
    # The card carries the reviewer's reason.
    assert asked[0]["review"] == {"decision": "ask", "reason": "You didn't ask to email anyone."}


def test_full_autonomy_allows_without_asking_anyone_and_records_it(reviewer):
    asked, callback = _person(False)
    outcome, line = approval_gate.resolve_approval_line(dict(_PAYLOAD), approval_mode="full",
                                                        approval_callback=callback)
    assert (outcome, line) == ("allow", "Approval: allowed by Full autonomy")
    assert asked == [] and reviewer["seen"] == []


@pytest.mark.parametrize("mode", ["approve", "auto", "full"])
def test_read_only_actions_never_reach_the_reviewer_or_the_person(reviewer, mode):
    asked, callback = _person(False)
    outcome, line = approval_gate.resolve_approval_line(dict(_PAYLOAD), approval_mode=mode, read_only=True,
                                                        approval_callback=callback)
    assert (outcome, line) == ("allow", "Approval: not needed (read-only)")
    assert asked == [] and reviewer["seen"] == []


def test_the_reviewers_decision_is_recorded_on_a_tool_result_that_has_no_line_of_its_own(reviewer):
    with approval_gate.collect_decisions() as decisions:
        assert approval_gate.request_approval(dict(_PAYLOAD), approval_mode="auto") is True
    assert approval_gate.with_decisions(decisions, "Pushed main.") == (
        "Approval: approved by the reviewer: You asked to email Pat\nPushed main.")
    # A result that leads with its own line keeps it; structured results stay data.
    assert approval_gate.with_decisions(decisions, "Approval: asked; approved by you\nx").count("Approval:") == 1
    assert approval_gate.with_decisions(decisions, '{"ok": true}') == '{"ok": true}'

    with approval_gate.collect_decisions() as decisions:
        approval_gate.request_approval(dict(_PAYLOAD), approval_mode="full")
    assert approval_gate.with_decisions(decisions, "Done.") == "Approval: allowed by Full autonomy\nDone."


def test_a_resumed_run_takes_the_persons_answer_and_never_asks_the_reviewer_again(reviewer):
    """LangGraph runs a tool again from the start once the person answers: the reviewer must not decide on that
    re-run, or it could approve what the person denied."""
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.graph import END, START, StateGraph
    from langgraph.types import Command

    class State(TypedDict):
        results: Annotated[list, operator.add]

    def node(_state):
        allowed = approval_gate.request_approval(dict(_PAYLOAD), approval_mode="auto")
        return {"results": [allowed]}

    builder = StateGraph(State)
    builder.add_node("act", node)
    builder.add_edge(START, "act")
    builder.add_edge("act", END)
    graph = builder.compile(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "resume-check"}}

    reviewer["verdict"] = ReviewVerdict("ask", "Not what you asked for.")
    first = graph.invoke({"results": []}, config)
    [pending] = first["__interrupt__"]
    assert pending.value["review"] == {"decision": "ask", "reason": "Not what you asked for."}
    assert len(reviewer["seen"]) == 1

    # Were the reviewer asked again it would now approve; the person's "no" must stand.
    reviewer["verdict"] = ReviewVerdict("approve", "Looks fine.")
    final = graph.invoke(Command(resume=False), config)
    assert final["results"] == [False]
    assert len(reviewer["seen"]) == 1


def test_the_card_and_a_paused_runs_message_say_why_the_reviewer_asked():
    from row_bot.application.approval_projection import project_approval_context
    from row_bot.approval_messages import compact_message, normalize_interrupts

    asked = {**_PAYLOAD, "review": {"decision": "ask", "reason": "You asked for a draft, not to send it."}}
    assert project_approval_context(asked)["reviewer_reason"] == "You asked for a draft, not to send it."
    assert "reviewer_reason" not in project_approval_context(dict(_PAYLOAD))  # Ask: nothing to say.
    message = compact_message(normalize_interrupts([asked], source_label="Helper"))
    assert "The reviewer asked you: You asked for a draft, not to send it." in message


def test_the_activity_reads_who_let_an_action_run():
    """The conversation's activity shows the result's approval line apart from the result (B235)."""
    from row_bot.application.conversation_traces import classify_tool_result

    for line in ("Approval: approved by the reviewer: You asked for it", "Approval: allowed by Full autonomy"):
        assert classify_tool_result(f"{line}\nSent.") == "succeeded"
    assert classify_tool_result(
        "Approval: asked; denied by you — did not run. The reviewer asked you: Not asked\nAction cancelled by user."
    ) == "cancelled"
