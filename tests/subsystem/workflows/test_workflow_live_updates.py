"""A background workflow run tells every open client at once (B407).

Its start, its approval and its end move the `runs`, `conversations` and
`approvals` topics, so the sidebar, Home and Monitor read them again straight
away instead of on their next poll.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from row_bot.application import live_updates as live_module
from row_bot.application.live_updates import LiveUpdates
from tests.contracts.client_platform.test_headless_lifecycle import platform  # noqa: F401
from tests.subsystem.workflows.test_workflow_runs_as_chat import (  # noqa: F401
    X_POST, Recording, create, pending_approval, posted, resolve, run, settle, workflows,
)

pytestmark = pytest.mark.subsystem


@pytest.fixture
def live(monkeypatch):
    fresh = LiveUpdates()
    monkeypatch.setattr(live_module, "live_updates", fresh)
    return fresh


def _moved(live: LiveUpdates, before: dict) -> set[str]:
    after = live.frame()["topics"]
    return {topic for topic, revision in after.items() if revision != before[topic]}


def test_a_background_run_publishes_its_start_approval_and_end(workflows, platform, live):  # noqa: F811
    task = create(workflows)

    before = live.frame()["topics"]
    waiting = run(workflows, platform, Recording((("interrupt", X_POST),), posted()), task)
    # It started in its own conversation and now waits on the person.
    assert waiting["status"] == "waiting"
    assert {"runs", "conversations", "approvals"} <= _moved(live, before)

    before = live.frame()["topics"]
    resolve(platform, pending_approval(workflows, waiting["thread_id"]), "approve", "approve-post")
    settle(platform, waiting["thread_id"])
    # Answered, the run went on and finished.
    assert {"runs", "conversations", "approvals"} <= _moved(live, before)
    assert workflows.get_run_history(task["id"])[0]["status"] == "completed"


def test_an_expired_approval_publishes_and_an_idle_check_does_not(workflows, live, monkeypatch):  # noqa: F811
    workflows.create_approval_request("run-1", "", "helper", "Approve?", timeout_minutes=5,
                                      agent_run_id="run-1", resume_kind="agent_run")
    monkeypatch.setattr("row_bot.agent_runner.resume_agent_run", lambda *a, **kw: None)

    # The scheduler checks often; a check that changes nothing says nothing.
    before = live.frame()["topics"]
    workflows._check_approval_timeouts()
    assert _moved(live, before) == set()

    conn = workflows._get_conn()  # Ten minutes on.
    conn.execute("UPDATE approval_requests SET timeout_at = ?",
                 ((datetime.now() - timedelta(minutes=5)).isoformat(),))
    conn.commit()
    conn.close()
    workflows._check_approval_timeouts()
    assert {"approvals", "conversations"} <= _moved(live, before)


def test_a_new_conversation_and_a_rename_publish_the_list(workflows, live):  # noqa: F811
    from row_bot import threads
    before = live.frame()["topics"]
    thread_id = threads.create_thread("Trip planning", seed_default_skills=False)
    assert _moved(live, before) == {"conversations"}
    before = live.frame()["topics"]
    threads._save_thread_meta(thread_id, "Trip to Lisbon")
    assert _moved(live, before) == {"conversations"}
