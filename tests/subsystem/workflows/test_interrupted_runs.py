from __future__ import annotations

from datetime import datetime

import pytest

from tests.fixtures.tasks import fresh_tasks_module

pytestmark = pytest.mark.subsystem

CUTOFF = datetime(2026, 9, 26, 18, 0)


@pytest.fixture
def runs(tmp_path, reload_for_data_dir):
    tasks = fresh_tasks_module(tmp_path, reload_for_data_dir)
    conn = tasks._get_conn()
    try:
        conn.execute(
            "INSERT INTO tasks (id,name,description,icon,prompts,created_at,enabled) "
            "VALUES ('task-a','Synthetic task','','','[\"Synthetic prompt\"]','2026-01-01',1)"
        )
        conn.executemany(
            "INSERT INTO task_runs(id,task_id,thread_id,started_at,status,steps_total,steps_done) "
            "VALUES (?,?,?,?,?,?,?)",
            [
                ("old-running", "task-a", "t1", "2026-08-06T09:00:00.075057", "running", 4, 1),
                ("old-starting", "task-a", "t2", "2026-08-07T09:00:00", "starting", 4, 0),
                ("old-stopping", "task-a", "t3", "2026-08-08T09:00:00", "stopping", 4, 2),
                ("old-waiting", "task-a", "t4", "2026-08-09T09:00:00", "waiting", 4, 2),
                ("old-completed", "task-a", "t5", "2026-08-10T09:00:00", "completed", 4, 4),
                ("new-running", "task-a", "t6", "2026-09-26T18:00:05", "running", 4, 1),
            ],
        )
        conn.commit()
    finally:
        conn.close()
    return tasks


def statuses(tasks):
    conn = tasks._get_conn()
    try:
        return {
            row["id"]: (row["status"], row["status_message"], row["finished_at"])
            for row in conn.execute("SELECT id,status,status_message,finished_at FROM task_runs")
        }
    finally:
        conn.close()


def test_runs_left_unfinished_by_an_earlier_process_are_marked_stopped(runs):
    settled = runs.settle_interrupted_runs(CUTOFF)

    assert settled == ["old-running", "old-starting", "old-stopping"]
    after = statuses(runs)
    for run_id in settled:
        status, message, finished_at = after[run_id]
        assert status == "stopped"
        assert message.startswith("Interrupted: Row-Bot closed")
        assert finished_at
    # A run waiting on its card goes on when the card is answered; this process's runs are untouched.
    assert after["old-waiting"][0] == "waiting"
    assert after["new-running"][0] == "running"
    assert after["old-completed"][0] == "completed"
    assert runs.settle_interrupted_runs(CUTOFF) == []


def test_scheduler_start_settles_once_without_delivering(runs, monkeypatch):
    calls = []
    monkeypatch.setattr(runs, "_get_scheduler", lambda: None)
    monkeypatch.setattr(runs, "sync_all_jobs", lambda: None)
    monkeypatch.setattr(runs, "start_approval_monitor", lambda: None)
    monkeypatch.setattr(runs, "_PROCESS_STARTED_AT", CUTOFF)
    original = runs.settle_interrupted_runs
    monkeypatch.setattr(
        runs,
        "settle_interrupted_runs",
        lambda before=None: calls.append(before) or original(before),
    )
    for name in ("run_task_background", "get_task_channels"):
        monkeypatch.setattr(
            runs, name, lambda *a, **k: pytest.fail("settling started or delivered")
        )

    runs.start_task_scheduler()
    runs.start_task_scheduler()

    assert calls == [None]
    assert statuses(runs)["old-running"][0] == "stopped"
    assert statuses(runs)["new-running"][0] == "running"
