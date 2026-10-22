"""N9 "Always ask me": the person's own rule for a workflow's step, tool or app.

Such an approval is person-only: the Auto reviewer never decides it and Hands-off never skips it.
It is bound to the exact arguments on its card: an answer for other details never runs the
action, and a changed argument asks again. These are security tests: they stay in the pull
request lane.
"""

from __future__ import annotations

import operator
import threading
from typing import Annotated, TypedDict

import pytest

from row_bot import approval_review
from row_bot.approval_review import ReviewVerdict
from row_bot.tools import approval_gate

pytestmark = pytest.mark.subsystem

_POST = {"tool": "x_post", "label": "Post on X", "description": "Post on X: Launch day!"}


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


def _person(*answers):
    asked: list[dict] = []
    queue = list(answers)

    def callback(payload):
        asked.append(payload)
        return queue.pop(0)
    return asked, callback


@pytest.mark.parametrize("mode", ["approve", "auto", "full", "block"])
def test_only_the_person_answers_under_every_level(reviewer, level, mode):
    level(mode)
    binding = approval_gate.call_binding("x_post", {"text": "Launch day!"})
    asked, callback = _person(approval_gate.bound_answer(True, binding))

    with approval_gate.collect_decisions() as lines:
        approved, line = approval_gate.person_approval(dict(_POST), binding=binding, approval_callback=callback)

    assert (approved, line) == (True, approval_gate.APPROVAL_GIVEN)
    assert lines == [approval_gate.APPROVAL_GIVEN]
    assert reviewer == []  # The reviewer never decides it, and Hands-off never skips it.
    [card] = asked
    assert card["person_only"] is True and card["binding"] == binding
    assert card["review"] == {"decision": "ask", "reason": "You asked to approve this yourself."}


@pytest.mark.parametrize("answer", [True, "take over", {"approved": True}])
def test_an_answer_not_bound_to_the_card_never_runs_it(reviewer, level, answer):
    """Only the person's decision on this card counts: no level's plain "yes" does."""
    level("full")
    binding = approval_gate.call_binding("x_post", {"text": "Launch day!"})
    _asked, callback = _person(answer)

    approved, line = approval_gate.person_approval(dict(_POST), binding=binding, approval_callback=callback)

    assert (approved, line) == (False, approval_gate.APPROVAL_DENIED)


def test_an_approval_for_other_details_never_runs_the_action(level):
    level("approve")
    approved_text = approval_gate.call_binding("x_post", {"text": "Launch day!"})
    changed_text = approval_gate.call_binding("x_post", {"text": "Launch day! Buy now"})
    _asked, callback = _person(approval_gate.bound_answer(True, approved_text))

    approved, line = approval_gate.person_approval(dict(_POST), binding=changed_text, approval_callback=callback)

    assert (approved, line) == (False, approval_gate.APPROVAL_CHANGED)


def test_the_binding_covers_every_argument_and_the_medias_contents():
    base = approval_gate.call_binding("x_post", {"text": "Hi", "media": ["a.png"]}, ["0a1b2c3d4e5f"])

    assert base == approval_gate.call_binding("x_post", {"media": ["a.png"], "text": "Hi"}, ["0a1b2c3d4e5f"])
    assert base != approval_gate.call_binding("x_post", {"text": "Hi!", "media": ["a.png"]}, ["0a1b2c3d4e5f"])
    assert base != approval_gate.call_binding("x_post", {"text": "Hi", "media": ["a.png"]}, ["ffffffffffff"])


def _graph(calls):
    """A graph whose one task (one tool call) asks the person about each call in turn."""
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.graph import END, START, StateGraph

    class State(TypedDict):
        results: Annotated[list, operator.add]

    def node(_state):
        outcomes = []
        for text in calls:
            binding = approval_gate.call_binding("x_post", {"text": text})
            outcomes.append(approval_gate.person_approval(
                {**_POST, "description": f"Post on X: {text}"}, binding=binding)[0])
        return {"results": [outcomes]}

    builder = StateGraph(State)
    builder.add_node("act", node)
    builder.add_edge(START, "act")
    builder.add_edge("act", END)
    return builder.compile(checkpointer=InMemorySaver())


def test_in_a_run_hands_off_pauses_for_the_person_and_their_answer_is_kept(reviewer, level):
    from langgraph.types import Command

    level("full")
    graph = _graph(["Launch day!"])
    config = {"configurable": {"thread_id": "always-ask"}}

    first = graph.invoke({"results": []}, config)

    [pending] = first["__interrupt__"]
    assert pending.value["person_only"] is True
    final = graph.invoke(Command(resume=approval_gate.bound_answer(True, pending.value["binding"])), config)
    assert final["results"] == [[True]]
    assert reviewer == []


def test_a_changed_argument_asks_again(level):
    """The person approved one post; a second post with other text is another question."""
    from langgraph.types import Command

    level("auto")
    graph = _graph(["Launch day!", "Launch day! Buy now"])
    config = {"configurable": {"thread_id": "changed"}}

    first = graph.invoke({"results": []}, config)
    [pending] = first["__interrupt__"]
    second = graph.invoke(Command(resume=approval_gate.bound_answer(True, pending.value["binding"])), config)

    [asked_again] = second["__interrupt__"]
    assert asked_again.value["description"] == "Post on X: Launch day! Buy now"
    assert asked_again.value["binding"] != pending.value["binding"]
    final = graph.invoke(Command(resume=approval_gate.bound_answer(False, asked_again.value["binding"])), config)
    assert final["results"] == [[True, False]]


def test_inside_an_approved_call_the_tool_does_not_ask_twice(reviewer):
    asked, callback = _person()

    with approval_gate.approved_by_person("binding"):
        outcome, line = approval_gate.resolve_approval_line(dict(_POST), approval_mode="approve",
                                                            approval_callback=callback)

    assert (outcome, line) == ("allow", approval_gate.APPROVAL_GIVEN)
    assert asked == [] and reviewer == []


def test_the_run_rules_hold_only_what_they_name_and_never_a_read(monkeypatch, level):
    from row_bot import agent
    from row_bot.tools import action_card

    level("full")
    monkeypatch.setattr(action_card, "_account", lambda *_a, **_k: "X account")
    asked: list[dict] = []

    def person(payload, *, binding, **_kwargs):
        asked.append(payload)
        return False, approval_gate.APPROVAL_DENIED
    monkeypatch.setattr(approval_gate, "person_approval", person)

    agent._current_person_rules_var.set({"step": False, "items": ["x"]})
    held, _binding = agent._person_rule_refusal("x_post", {"text": "Launch day!"}, source="core", parent="x")
    free, _ = agent._person_rule_refusal("workspace_write_file", {"path": "a.md"}, source="core", parent="filesystem")
    agent._current_person_rules_var.set({"step": True, "items": []})
    read, _ = agent._person_rule_refusal("workspace_read_file", {"path": "a.md"}, source="core", parent="filesystem")
    agent._current_person_rules_var.set(None)

    assert held.startswith(approval_gate.APPROVAL_DENIED) and "did not run" in held
    assert free is None and read is None
    [card] = asked
    assert card["card"]["account"] == "X account"
    assert card["card"]["text"] == "Launch day!"


def test_the_card_names_media_by_name_size_and_fingerprint(tmp_path):
    from row_bot.tools.action_card import describe_action

    image = tmp_path / "launch.png"
    content = b"\x89PNG fake image bytes"
    image.write_bytes(content)

    card = describe_action("x_post", {"text": "Launch day!", "media": [str(image)], "to": "@rowbot"})

    assert card["destination"] == "@rowbot" and card["text"] == "Launch day!"
    [media] = card["media"]
    assert (media["name"], media["size"], media["kind"]) == ("launch.png", len(content), "image")
    assert len(media["fingerprint"]) == 12


def test_an_agent_the_workflow_starts_keeps_its_rules_and_only_the_person_answers(
    tmp_path, monkeypatch, reload_for_data_dir,
):
    """A helper started from a workflow carries "Always ask me": its card is person-only, expires in a day,
    and the person's approval resumes it bound to what the card showed."""
    import importlib
    import json

    from tests.subsystem.agents.test_agent_runner import _fresh_agent_runner_modules

    agent_runner, _runs, _profiles, _context, threads = _fresh_agent_runner_modules(
        tmp_path, monkeypatch, reload_for_data_dir)
    tasks = importlib.import_module("row_bot.tasks")
    parent = threads.create_thread("Launch workflow run", approval_mode="full")
    binding = approval_gate.call_binding("x_post", {"text": "Launch day!"})
    seen: dict[str, object] = {}
    resumed = threading.Event()

    def fake_interrupt(prompt, enabled_tool_names, config, *, stop_event):
        seen["rules"] = config["configurable"].get("person_rules")
        return {"type": "interrupt", "interrupts": [{
            "tool": "x_post", "label": "Post on X", "description": "Post on X: Launch day!",
            "person_only": True, "binding": binding, "card": {"action": "Post on X", "text": "Launch day!"}}]}

    def fake_resume(enabled_tool_names, config, approved, *, interrupt_ids=None, stop_event):
        seen["answer"] = approved
        resumed.set()
        return "Posted."

    monkeypatch.setattr(agent_runner, "_invoke_agent", fake_interrupt)
    monkeypatch.setattr(agent_runner, "_resume_invoke_agent", fake_resume)
    from row_bot import agent

    agent._current_person_rules_var.set({"step": False, "items": ["builtin:account:x"]})
    try:
        run = agent_runner.spawn_agent_run("Post the launch on X.", parent_thread_id=parent,
                                           enabled_tool_names=["x"], wait=True)
    finally:
        agent._current_person_rules_var.set(None)

    assert seen["rules"] == {"step": False, "items": ["builtin:account:x"]}
    assert run["status"] == "waiting_approval"
    [approval] = tasks.get_pending_approvals(agent_run_id=run["id"])
    payload = json.loads(approval["approval_payload_json"])
    assert (payload["person_only"], payload["binding"]) == (True, binding)
    assert approval["timeout_at"]  # It expires at the default timeout (a day).

    assert tasks.respond_to_approval(approval["resume_token"], True) is True
    assert resumed.wait(5)
    assert seen["answer"] == approval_gate.bound_answer(True, binding)
