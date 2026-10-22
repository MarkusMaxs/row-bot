"""A helper's question shows in the chat that started it, like its approvals (N4, B348).

The person answers it (or skips it) there; the helper goes on with that
answer for its question alone. Skipping never stops a helper, and an
approval never answers its question.
"""
from __future__ import annotations

import pytest

from row_bot import questions
from tests.subsystem.agents.test_agent_runner import _fresh_agent_runner_modules

pytestmark = pytest.mark.subsystem

_BETA = {"header": "Beta timing", "question": "When should the beta go out?",
         "options": [{"label": "Beta.1 now (Recommended)"}, {"label": "After the fixes"}]}


@pytest.fixture
def helper_asked(tmp_path, monkeypatch, reload_for_data_dir):
    agent_runner, agent_runs, _profiles, _context, threads = _fresh_agent_runner_modules(
        tmp_path, monkeypatch, reload_for_data_dir)
    parent = threads.create_thread("Plan the beta", approval_mode="approve")
    item = {**questions.card(questions.normalize([_BETA])), "__interrupt_id": "q-helper"}
    resumed: list[dict] = []
    monkeypatch.setattr(agent_runner, "_invoke_agent",
                        lambda prompt, tools, config, *, stop_event: {"type": "interrupt", "interrupts": [item]})

    def resume(tools, config, approved, *, interrupt_ids=None, resume_values=None, stop_event):
        resumed.append(resume_values)
        return "Planned it."

    monkeypatch.setattr(agent_runner, "_resume_invoke_agent", resume)
    run = agent_runner.spawn_agent_run("Plan the beta.", parent_thread_id=parent, profile="worker",
                                       enabled_tool_names=["shell"], wait=True)
    assert run["status"] == "waiting_approval"
    return agent_runner, agent_runs, parent, run, item, resumed


def _waiting(parent: str) -> dict:
    from row_bot.application.client_monitor import read_pending_approvals

    [waiting] = [item for item in read_pending_approvals()["items"] if item["parent_conversation_id"] == parent]
    return waiting


def test_a_helpers_question_shows_in_the_parent_chat_as_a_question(helper_asked):
    from row_bot.application.client_platform import client_platform_service
    from row_bot.approval_messages import compact_message, payload_from_row
    from row_bot.tasks import get_pending_approvals

    _runner, _runs, parent, _run, _item, _resumed = helper_asked
    waiting = _waiting(parent)
    assert waiting["kind"] == "question"
    card = client_platform_service.get_approval(waiting["id"])
    assert card["questions"][0]["question"] == "When should the beta go out?"
    [row] = get_pending_approvals(approval_id=waiting["id"])
    assert compact_message(payload_from_row(row)).endswith("has a question: When should the beta go out?")


def test_the_helper_goes_on_with_the_answer(helper_asked):
    from row_bot.tasks import get_pending_approvals, respond_to_approval

    agent_runner, agent_runs, parent, run, item, resumed = helper_asked
    [row] = get_pending_approvals(approval_id=_waiting(parent)["id"])
    assert respond_to_approval(row["resume_token"], True) is False  # an approval never answers it

    answer = questions.answer(item, [{"choices": [1], "other": ""}])
    assert respond_to_approval(row["resume_token"], True, answer=answer) is True
    agent_runner.wait_for_agent_run(run["id"], timeout=10)

    assert resumed == [{"q-helper": answer}]
    assert agent_runs.get_agent_run(run["id"])["status"] == "completed"


def test_skipping_a_helpers_question_never_stops_it(helper_asked):
    from row_bot.tasks import get_pending_approvals, respond_to_approval

    agent_runner, agent_runs, parent, run, _item, resumed = helper_asked
    [row] = get_pending_approvals(approval_id=_waiting(parent)["id"])

    assert respond_to_approval(row["resume_token"], False) is True
    agent_runner.wait_for_agent_run(run["id"], timeout=10)

    assert resumed == [{"q-helper": questions.skipped()}]
    assert agent_runs.get_agent_run(run["id"])["status"] == "completed"
