"""A helper started from an incognito chat asks about each memory write by itself (N11, B415).

A helper that saved two memories in one step showed one card; the person's answer went to both
writes, bound to the first one's text, so the second was neither saved nor asked again, and a Don't
save stopped the helper. Now, as in a chat (B412), each write is its own "Save to memory?" card in the
helper's notice in the parent chat, answered by the person alone, and a Don't save on one leaves the
helper going on. These run the real graph, approval gate and memory tool with a fake model: they are
security tests and stay in the pull request lane.
"""
from __future__ import annotations

import pytest

from row_bot.tools import approval_gate
from tests.fixtures.memory_stack import fresh_memory_stack
from tests.subsystem.agents.test_agent_runner import _fresh_agent_runner_modules
from tests.subsystem.agents.test_incognito_memory_approval import TEXTS, WRITES, level, reviewer  # noqa: F401
from tests.subsystem.client_platform.test_finished_turns_answer_every_call import ProviderShapedModel

pytestmark = pytest.mark.subsystem


def _settled(graph, config):
    """What the agent hands a helper's runner: the waiting interrupts, or the answer."""
    from row_bot.agent import _graph_interrupt_result

    state = graph.get_state(config)
    return _graph_interrupt_result(state) or state.values["messages"][-1].content


@pytest.fixture
def helper(tmp_path, monkeypatch, reload_for_data_dir):
    """A helper from an incognito chat whose one step makes two memory writes, waiting on its first card."""
    from langchain_core.messages import AIMessage, HumanMessage
    from langchain_core.tools import tool
    from langgraph.prebuilt import create_react_agent
    from langgraph.types import Command

    stack = fresh_memory_stack(tmp_path, monkeypatch, reload_for_data_dir)
    agent_runner, agent_runs, _profiles, _context, threads = _fresh_agent_runner_modules(
        tmp_path, monkeypatch, reload_for_data_dir)
    parent = threads.create_thread("Private", seed_default_skills=False, incognito=True)
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
    graph = create_react_agent(model=model, tools=[save_memory], checkpointer=threads.checkpointer, version="v2")

    def invoke(prompt, tools, config, *, stop_event):
        graph.invoke({"messages": [HumanMessage(content=prompt)]}, config)
        return _settled(graph, config)

    def resume(tools, config, approved, *, interrupt_ids=None, resume_values=None, stop_event):
        from row_bot.agent import _resume_value

        # As the agent resumes a helper's graph with the runner's answer.
        graph.invoke(Command(resume=_resume_value(approved, interrupt_ids, resume_values)), config)
        return _settled(graph, config)

    monkeypatch.setattr(agent_runner, "_invoke_agent", invoke)
    monkeypatch.setattr(agent_runner, "_resume_invoke_agent", resume)
    run = agent_runner.spawn_agent_run("Remember how I take coffee and tea.", parent_thread_id=parent,
                                       profile="worker", enabled_tool_names=["memory"], wait=True)
    assert run["status"] == "waiting_approval"
    assert threads.incognito_state(run["thread_id"]).incognito
    return agent_runner, agent_runs, threads, stack, parent, run


def _waiting_card(parent: str) -> tuple[dict, str]:
    """The one card waiting in the parent chat for the helper: its row and the write it shows."""
    from row_bot.application.client_monitor import read_pending_approvals
    from row_bot.application.client_platform import client_platform_service
    from row_bot.tasks import get_pending_approvals

    [waiting] = [item for item in read_pending_approvals()["items"] if item["parent_conversation_id"] == parent]
    assert waiting["source"] == "agent" and waiting["kind"] == "approval"
    card = client_platform_service.get_approval(waiting["id"])
    # The N11 card: its question and exact text, for this one write; nothing about a reviewer.
    assert card["question"] == approval_gate.INCOGNITO_MEMORY_QUESTION
    assert card["memory_text"] in TEXTS
    assert card["scope"] == "Only this requested action will be resolved."
    assert "reviewer_reason" not in card
    # Its notice names that write alone, not "continue with 2 actions", and no reviewer asked.
    assert card["memory_text"] in waiting["what"]
    assert "2 actions" not in waiting["what"] and "reviewer" not in waiting["what"]
    [row] = get_pending_approvals(approval_id=waiting["id"])
    return row, card["memory_text"]


def _answer(agent_runner, run: dict, row: dict, approved: bool) -> dict:
    from row_bot.tasks import respond_to_approval

    assert respond_to_approval(row["resume_token"], approved) is True
    return agent_runner.wait_for_agent_run(run["id"], timeout=10)


def _saved(stack) -> set[str]:
    return {f"{subject}: {content}" for subject, content in WRITES.items()
            if stack["memory"].find_by_subject("preference", subject) is not None}


def _notices(threads, parent: str) -> list[str]:
    """The helper's notices in the parent chat, one per card."""
    return [str(message.content) for message in threads.get_latest_checkpoint_messages(parent)
            if (getattr(message, "additional_kwargs", {}).get("row_bot_ui") or {}).get("agent_approval_for")]


@pytest.mark.parametrize("mode", ["approve", "auto"])
def test_a_helpers_two_writes_are_two_cards_each_answered_by_itself(helper, reviewer, level, mode):  # noqa: F811
    agent_runner, _runs, threads, stack, parent, run = helper
    level(mode)
    first, shown = _waiting_card(parent)

    after_save = _answer(agent_runner, run, first, True)

    # Saved is only what that card showed; the other write asks for itself, on its own card.
    assert after_save["status"] == "waiting_approval"
    assert _saved(stack) == {shown}
    second, other = _waiting_card(parent)
    assert {shown, other} == TEXTS

    done = _answer(agent_runner, run, second, True)

    assert done["status"] == "completed" and done["summary"] == "Saved what you chose."
    assert _saved(stack) == TEXTS
    notices = _notices(threads, parent)
    assert len(notices) == 2 and shown in notices[0] and other in notices[1]
    assert reviewer == []  # No level and no reviewer answered either one.


def test_dont_save_on_one_helper_write_still_asks_about_the_other(helper, level):  # noqa: F811
    agent_runner, _runs, _threads, stack, parent, run = helper
    level("approve")
    first, declined = _waiting_card(parent)

    after_decline = _answer(agent_runner, run, first, False)

    assert after_decline["status"] == "waiting_approval"  # One Don't save never ends the helper's run.
    assert _saved(stack) == set()
    second, kept = _waiting_card(parent)
    assert kept != declined

    done = _answer(agent_runner, run, second, True)

    assert done["status"] == "completed"
    assert _saved(stack) == {kept}
