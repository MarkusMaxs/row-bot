"""Monitor's Workflows line, which Home's Today shows when it warns, counts workflows (B414).

A new profile with its five default workflows (all run manually) read "5 scheduled · 0 running ·
1 approval waiting" under "Needs attention": "scheduled" counted Row-Bot's own upkeep jobs on the
scheduler, and the approval was a chat's memory card answered minutes before, kept from the last
15-minute run until a manual Run diagnosis.
"""
from __future__ import annotations

import pytest

from row_bot import status_checks
from row_bot.application import client_diagnosis
from tests.subsystem.workflows.test_approvals_wait import _approval_step, workflows  # noqa: F401

pytestmark = pytest.mark.subsystem


@pytest.fixture
def scheduler(workflows, monkeypatch):  # noqa: F811
    """The app's scheduler, not started, holding Row-Bot's own upkeep jobs as start-up adds them."""
    from apscheduler.schedulers.background import BackgroundScheduler

    tasks = workflows[0]
    scheduler = BackgroundScheduler()
    for job_id in ("browser_idle_eviction", "checkpoint_cleanup", "oauth_token_health",
                   client_diagnosis.LOCAL_JOB):
        scheduler.add_job(lambda: None, "interval", minutes=15, id=job_id)
    monkeypatch.setattr(tasks, "_scheduler", scheduler)
    return scheduler


def _workflows_line() -> dict:
    [line] = [check for check in client_diagnosis.read_system_health()["checks"] if check["id"] == "workflows"]
    return line


def test_scheduled_counts_workflows_on_a_schedule_not_row_bots_own_jobs(workflows, scheduler):  # noqa: F811
    tasks = workflows[0]
    tasks.seed_default_tasks()  # Five workflows, each run manually.

    result = status_checks.check_task_scheduler()

    assert len(scheduler.get_jobs()) == 4
    assert (result.status, result.detail) == ("ok", "0 scheduled · 0 running · 0 approvals waiting")

    tasks.create_task("Daily news", steps=_approval_step(), schedule="daily:09:00", channels=[])
    tasks.create_task("Paused digest", steps=_approval_step(), schedule="daily:10:00", channels=[], enabled=False)

    assert status_checks.check_task_scheduler().detail == "1 scheduled · 0 running · 0 approvals waiting"


def test_a_chats_approval_is_not_a_workflow_approval(workflows, scheduler, monkeypatch):  # noqa: F811
    tasks = workflows[0]
    monkeypatch.setattr(tasks, "_resume_pipeline", lambda token, approved=True: None)
    # A chat's memory card and a helper's approval wait on their own cards, in their chats.
    tasks.create_approval_request("pass-1", "", "conversation", "Save to memory?", resume_kind="conversation",
                                  source_thread_id="chat-1")
    tasks.create_approval_request("run-helper", "", "agent_interrupt", "Helper needs approval",
                                  agent_run_id="run-helper", resume_kind="agent_run", parent_thread_id="chat-1")

    assert status_checks.check_task_scheduler().detail == "0 scheduled · 0 running · 0 approvals waiting"

    tasks.create_approval_request("run-1", "task-1", "review", "Send today's news?")

    result = status_checks.check_task_scheduler()
    assert (result.status, result.detail) == ("warn", "0 scheduled · 0 running · 1 approval waiting")


def test_an_answered_workflow_approval_clears_the_warning_at_once(workflows, scheduler, monkeypatch):  # noqa: F811
    tasks, threads, _clock, _notices = workflows
    monkeypatch.setattr(status_checks, "LOCAL_CHECKS", (status_checks.check_task_scheduler,))
    task_id = tasks.create_task("Morning digest", steps=_approval_step(), channels=[], enabled=False)
    tasks.run_task_background(task_id, threads.create_thread(task_id, seed_default_skills=False), [],
                              notification=False)
    client_diagnosis.run_local_checks()  # The 15-minute run keeps the line.
    assert (_workflows_line()["status"], _workflows_line()["detail"]) == (
        "warn", "0 scheduled · 0 running · 1 approval waiting")

    [waiting] = tasks.get_pending_approvals()
    assert tasks.respond_to_approval(waiting["resume_token"], True) is True

    # Monitor and Home read it again (on the approvals update), with no new run of the checks.
    line = _workflows_line()
    assert (line["status"], line["detail"], line["fix"]) == ("ok", "0 scheduled · 0 running · 0 approvals waiting",
                                                             None)
    assert tasks.get_run_history(task_id)[0]["status"] == "completed"
