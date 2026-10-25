"""A background workflow run tells every open client at once (B407).

Its start, its approval and its end move the `runs`, `conversations` and
`approvals` topics, so the sidebar, Home and Monitor read them again straight
away instead of on their next poll.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from row_bot.application import live_updates as live_module
from row_bot.application.live_updates import LiveUpdates
from tests.subsystem.workflows.test_approvals_wait import _approval_step, workflows  # noqa: F401

pytestmark = pytest.mark.subsystem


@pytest.fixture
def live(monkeypatch):
    fresh = LiveUpdates()
    monkeypatch.setattr(live_module, "live_updates", fresh)
    return fresh


def _moved(live: LiveUpdates, before: dict) -> set[str]:
    after = live.frame()["topics"]
    return {topic for topic, revision in after.items() if revision != before[topic]}


def test_a_background_run_publishes_its_start_approval_and_end(workflows, live):  # noqa: F811
    tasks, threads, _clock, _notices = workflows
    task_id = tasks.create_task("Morning digest", steps=_approval_step(), channels=[], enabled=False)

    before = live.frame()["topics"]
    thread_id = threads.create_thread(task_id, seed_default_skills=False)
    tasks.run_task_background(task_id, thread_id, [], notification=False)
    # It started in its own conversation and now waits on the person.
    assert {"runs", "conversations", "approvals"} <= _moved(live, before)
    [waiting] = tasks.get_pending_approvals()

    before = live.frame()["topics"]
    assert tasks.respond_to_approval(waiting["resume_token"], True) is True
    # Answered, the run went on and finished.
    assert {"runs", "conversations", "approvals"} <= _moved(live, before)
    assert tasks.get_run_history(task_id)[0]["status"] == "completed"


def test_an_expired_approval_publishes_and_an_idle_check_does_not(workflows, live, monkeypatch):  # noqa: F811
    tasks, _threads, clock, _notices = workflows
    monkeypatch.setattr(tasks, "_resume_pipeline", lambda token, approved=True: None)
    tasks.create_approval_request("run-1", "task-1", "approval_1", "Approve?", timeout_minutes=5)

    # The scheduler checks often; a check that changes nothing says nothing.
    before = live.frame()["topics"]
    tasks._check_approval_timeouts()
    assert _moved(live, before) == set()

    clock["now"] += timedelta(minutes=10)
    tasks._check_approval_timeouts()
    assert {"approvals", "conversations"} <= _moved(live, before)


def test_a_new_conversation_and_a_rename_publish_the_list(workflows, live):  # noqa: F811
    _tasks, threads, _clock, _notices = workflows
    before = live.frame()["topics"]
    thread_id = threads.create_thread("Trip planning", seed_default_skills=False)
    assert _moved(live, before) == {"conversations"}
    before = live.frame()["topics"]
    threads._save_thread_meta(thread_id, "Trip to Lisbon")
    assert _moved(live, before) == {"conversations"}
