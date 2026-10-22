"""Row-Bot's question in an unattended workflow run (N4).

The run waits like an approval step under every approval level: it shows in
Needs you and goes to the run's channels as the question. The person's answer
(or a skip) goes on with that question alone; nobody answering by the step's
timeout (24 hours unless the step says) goes on with the recommended option,
and a question with none stops the run with "No answer in time". An approval
never answers a question, and Approve / Reject on the run page never reach it.
"""
# ruff: noqa: F811 -- the imported runtime fixture is requested by name.
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from row_bot import questions
from row_bot.application import task_execution as execution
from tests.subsystem.workflows.test_task_execution import _fake_graph, runtime, start  # noqa: F401

pytestmark = pytest.mark.subsystem

_RECOMMENDED = {
    "header": "Beta timing", "question": "When should the beta go out?",
    "options": [{"label": "Beta.1 now (Recommended)"}, {"label": "After the fixes"}],
}
_OPEN = {"header": "Pages", "question": "Which pages go in?", "options": [{"label": "Plans"}, {"label": "Docs"}]}


def _asked(question: dict) -> dict:
    return {**questions.card(questions.normalize([question])), "__interrupt_id": "q-1"}


@pytest.fixture
def paused(runtime, monkeypatch):
    """Start a run whose step asks a question; returns (runtime, resumes, set the question)."""
    resumed: list[dict] = []
    asked = {"item": _asked(_RECOMMENDED)}

    def invoke(prompt, tools, config, stop_event=None):
        return {"type": "interrupt", "interrupts": [asked["item"]]}

    def resume(tools, config, approved=True, stop_event=None, resume_values=None):
        resumed.append(resume_values)
        return "Went on."

    _fake_graph(monkeypatch, invoke=invoke, resume=resume)
    return runtime, resumed, asked


def _pending(runtime) -> dict:
    [row] = runtime[0].get_pending_approvals()
    return row


@pytest.mark.parametrize("mode", ["approve", "auto", "full", "block"])
def test_every_approval_level_waits_for_the_person(paused, mode):
    from row_bot.application.client_monitor import read_pending_approvals

    runtime, resumed, _asked_ = paused
    runtime[0].update_task(runtime[3], safety_mode=mode)

    assert start(runtime).run.status == "paused"
    assert resumed == []
    row = _pending(runtime)
    waited = datetime.fromisoformat(row["timeout_at"]) - datetime.fromisoformat(row["requested_at"])
    assert timedelta(hours=23, minutes=59) < waited <= timedelta(hours=24)
    # Needs you lists it as a question, answered on its card, never with the run page's Approve / Reject.
    assert read_pending_approvals()["items"][0]["kind"] == "question"
    [card] = execution.list_task_approvals(runtime[3], "reviewed-run").items
    assert card.kind == "question" and card.response_available is False
    with pytest.raises(execution.TaskExecutionError):
        execution.respond_task_approval(runtime[3], "reviewed-run", card.id, expected_revision=card.revision,
                                        approved=True, validate=lambda: None)


def test_the_answer_goes_on_with_that_question_alone(paused):
    runtime, resumed, asked = paused
    start(runtime)
    token = _pending(runtime)["resume_token"]

    # An approval is never an answer.
    assert runtime[0].respond_to_approval(token, True) is False
    assert resumed == []

    answer = questions.answer(asked["item"], [{"choices": [1], "other": ""}])
    assert runtime[0].respond_to_approval(token, True, answer=answer, source="telegram") is True

    assert resumed == [{"q-1": answer}]
    assert runtime[0].get_pending_approvals() == []
    assert execution.get_task_run(runtime[3], "reviewed-run").status == "completed"


def test_skipping_goes_on_with_the_recommendation(paused):
    runtime, resumed, _asked_ = paused
    start(runtime)

    assert runtime[0].respond_to_approval(_pending(runtime)["resume_token"], False) is True

    assert resumed == [{"q-1": questions.skipped()}]


def _expire(runtime) -> None:
    conn = runtime[0]._get_conn()
    conn.execute("UPDATE approval_requests SET timeout_at = ?",
                 ((datetime.now() - timedelta(minutes=1)).isoformat(),))
    conn.commit()
    conn.close()


def test_no_answer_in_time_goes_on_with_the_recommended_option(paused):
    runtime, resumed, _asked_ = paused
    start(runtime)
    _expire(runtime)

    runtime[0]._check_approval_timeouts()

    assert resumed == [{"q-1": questions.timed_out()}]
    assert execution.get_task_run(runtime[3], "reviewed-run").status == "completed"
    told = questions.result(_asked(_RECOMMENDED), questions.timed_out())
    assert "Go on with your recommended option (Beta timing: Beta.1 now)" in told


def test_no_answer_in_time_to_a_question_without_a_recommendation_stops_the_run(paused):
    runtime, resumed, asked = paused
    asked["item"] = _asked(_OPEN)
    start(runtime)
    _expire(runtime)

    runtime[0]._check_approval_timeouts()

    assert resumed == []
    [run] = runtime[0].get_run_history(runtime[3])
    assert (run["status"], run["status_message"]) == ("stopped", "No answer in time")


def test_the_question_goes_to_the_runs_channels_as_the_question(paused, monkeypatch):
    from row_bot.channels import registry

    runtime, _resumed, _asked_ = paused
    sent: list = []

    class Channel:
        name = display_name = "chat"

        def get_default_target(self):
            return "synthetic-target"

        def send_approval_request(self, target, interrupt_data, config):
            sent.append(interrupt_data)
            return "message-1"

    monkeypatch.setattr(registry, "running_channels", lambda: [Channel()])
    runtime[0].update_task(runtime[3], channels=["chat"])
    start(runtime)

    [pushed] = sent
    assert questions.question_item(pushed)["questions"][0]["question"] == "When should the beta go out?"
