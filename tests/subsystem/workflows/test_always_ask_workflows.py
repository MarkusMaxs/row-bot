"""N9 "Always ask me" in a workflow run: the person's own card always pauses the run, even under
Hands-off; the person's answer resumes it bound to exactly what the card showed; an unanswered
card expires at the step's timeout (24 hours unless the step sets one) and the run reads "Not
approved in time". A step that sends a message asks before it sends. Security tests: PR lane.
"""

from __future__ import annotations

import contextvars
import json
import sys
import types
from datetime import datetime, timedelta

import pytest

pytestmark = pytest.mark.subsystem

BINDING = "b" * 64
CARD = {"action": "Post on X", "destination": "", "account": "X account", "text": "Launch day!",
        "media": [{"name": "launch.png", "size": 2048, "fingerprint": "0a1b2c3d4e5f", "kind": "image"}]}
PERSON_INTERRUPT = {"tool": "x_post", "label": "Post on X", "description": "Post on X: Launch day!",
                    "args": {"text": "Launch day!"}, "person_only": True, "binding": BINDING, "card": CARD}


@pytest.fixture
def run(tmp_path, monkeypatch, reload_for_data_dir):
    tasks, threads = reload_for_data_dir(tmp_path / "data", "row_bot.tasks", "row_bot.threads")
    seen: dict[str, list] = {"invoked": [], "resumed": [], "repaired": [], "delivered": []}
    agent = types.ModuleType("row_bot.agent")

    class TaskStoppedError(Exception):
        pass

    def invoke_agent(prompt, tools, config, stop_event=None):
        seen["invoked"].append(config["configurable"].get("person_rules"))
        return {"type": "interrupt", "interrupts": [dict(PERSON_INTERRUPT)]}

    def resume_invoke_agent(tools, config, approved, **_kwargs):
        seen["resumed"].append(approved)
        return "Posted."

    agent.TaskStoppedError = TaskStoppedError
    agent.invoke_agent = invoke_agent
    agent.resume_invoke_agent = resume_invoke_agent
    agent.repair_orphaned_tool_calls = lambda *a, **k: seen["repaired"].append(True)
    agent._approval_mode_var = contextvars.ContextVar("approval_mode", default="approve")
    agent._background_workflow_var = contextvars.ContextVar("background_workflow", default=False)
    agent._persistent_thread_var = contextvars.ContextVar("persistent_thread", default=False)
    monkeypatch.setitem(sys.modules, "row_bot.agent", agent)

    class ImmediateThread:
        def __init__(self, target, *args, **kwargs):
            self._target = target

        def start(self):
            self._target()

    monkeypatch.setattr(tasks.threading, "Thread", ImmediateThread)
    from row_bot import notifications

    monkeypatch.setattr(notifications, "notify", lambda **_kwargs: None)
    monkeypatch.setattr(tasks, "_push_approval_to_channels", lambda *a, **k: None)
    monkeypatch.setattr(tasks, "_deliver_to_channels", lambda *a, **k: ("not_configured", ""))
    monkeypatch.setattr(tasks, "_deliver_to_channel",
                        lambda task, text, **_k: seen["delivered"].append((task["delivery_channel"], text)))

    def start(steps, *, mode="full", always_ask=None):
        task_id = tasks.create_task("Launch", steps=steps, safety_mode=mode, always_ask=always_ask)
        thread_id = threads.create_thread("Launch run", seed_default_skills=False)
        tasks.run_task_background(task_id, thread_id, [], notification=False)
        [history] = tasks.get_run_history(task_id, limit=1)
        return task_id, history["id"]

    return types.SimpleNamespace(tasks=tasks, seen=seen, start=start)


def _pending(tasks, run_id):
    return [row for row in tasks.get_pending_approvals() if row["run_id"] == run_id]


def _run(tasks, task_id, run_id):
    from row_bot.application.task_execution import get_task_run

    return get_task_run(task_id, run_id)


@pytest.mark.parametrize("mode", ["full", "auto", "block"])
def test_the_persons_card_pauses_the_run_under_every_level(run, mode):
    task_id, run_id = run.start([{"type": "prompt", "prompt": "Post the launch", "always_ask": True}], mode=mode)

    [approval] = _pending(run.tasks, run_id)
    assert _run(run.tasks, task_id, run_id).status == "paused"
    assert run.seen["invoked"] == [{"step": True, "items": []}]
    assert run.seen["resumed"] == []  # Hands-off didn't approve it and Block didn't refuse it.
    payload = json.loads(approval["approval_payload_json"])
    assert (payload["person_only"], payload["binding"], payload["card"]) == (True, BINDING, CARD)
    # It expires at the step's timeout: 24 hours unless the step sets one.
    requested = datetime.fromisoformat(approval["requested_at"])
    assert datetime.fromisoformat(approval["timeout_at"]) - requested == pytest.approx(timedelta(hours=24),
                                                                                       abs=timedelta(seconds=5))


def test_the_workflows_app_rule_reaches_every_step(run):
    run.start([{"type": "prompt", "prompt": "Post the launch"}], always_ask=["x", "account:x"])

    assert run.seen["invoked"] == [{"step": False, "items": ["x", "account:x"]}]


@pytest.mark.parametrize("approved", [True, False])
def test_the_answer_resumes_the_action_bound_to_its_card(run, approved):
    task_id, run_id = run.start([{"type": "prompt", "prompt": "Post the launch", "always_ask": True}])
    [approval] = _pending(run.tasks, run_id)

    assert run.tasks.respond_to_approval(approval["resume_token"], approved) is True

    assert run.seen["resumed"] == [{"approved": approved, "binding": BINDING}]
    assert _run(run.tasks, task_id, run_id).status == "completed"


def test_an_unanswered_card_expires_and_the_run_reads_not_approved_in_time(run):
    task_id, run_id = run.start([{"type": "prompt", "prompt": "Post the launch", "always_ask": True,
                                  "timeout_minutes": 30}])
    [approval] = _pending(run.tasks, run_id)
    assert datetime.fromisoformat(approval["timeout_at"]) - datetime.fromisoformat(approval["requested_at"]) == (
        pytest.approx(timedelta(minutes=30), abs=timedelta(seconds=5)))
    conn = run.tasks._get_conn()
    try:
        conn.execute("UPDATE approval_requests SET timeout_at = ? WHERE id = ?",
                     ((datetime.now() - timedelta(minutes=1)).isoformat(), approval["id"]))
        conn.commit()
    finally:
        conn.close()

    run.tasks._check_approval_timeouts()

    summary = _run(run.tasks, task_id, run_id)
    assert (summary.status, summary.notice) == ("stopped", "Not approved in time")
    assert run.seen["resumed"] == []  # The action never ran.
    assert run.seen["repaired"] == [True]
    assert _pending(run.tasks, run_id) == []


def test_a_message_step_asks_before_it_sends_exactly_that_text(run):
    task_id, run_id = run.start([{"type": "notify", "channel": "telegram", "message": "Ship it today",
                                  "always_ask": True}])
    [approval] = _pending(run.tasks, run_id)
    payload = json.loads(approval["approval_payload_json"])
    assert payload["card"]["text"] == "Ship it today" and payload["person_only"] is True
    assert run.seen["delivered"] == []

    run.tasks.respond_to_approval(approval["resume_token"], True)

    assert run.seen["delivered"] == [("telegram", "Ship it today")]
    assert _run(run.tasks, task_id, run_id).status == "completed"


def test_a_message_the_person_denies_is_never_sent(run):
    task_id, run_id = run.start([{"type": "notify", "channel": "telegram", "message": "Ship it today",
                                  "always_ask": True}])
    [approval] = _pending(run.tasks, run_id)

    run.tasks.respond_to_approval(approval["resume_token"], False)

    summary = _run(run.tasks, task_id, run_id)
    assert (summary.status, summary.notice) == ("stopped", "You didn't approve it, so it didn't happen.")
    assert run.seen["delivered"] == []


def test_the_card_on_the_runs_page_says_exactly_what_will_happen(run):
    task_id, run_id = run.start([{"type": "prompt", "prompt": "Post the launch", "always_ask": True}])
    from row_bot.application.task_execution import list_task_approvals

    [item] = list_task_approvals(task_id, run_id).items

    assert item.person_only is True
    assert item.card == {"action": "Post on X", "destination": None, "account": "X account",
                         "text": "Launch day!", "media": CARD["media"]}


def test_a_channel_the_workflow_always_asks_about_waits_before_it_sends(run):
    task_id, run_id = run.start([{"type": "notify", "channel": "telegram", "message": "Ship it today"}],
                                always_ask=["builtin:channel:telegram"])

    [approval] = _pending(run.tasks, run_id)
    assert json.loads(approval["approval_payload_json"])["person_only"] is True
    assert run.seen["delivered"] == []
    assert _run(run.tasks, task_id, run_id).status == "paused"
