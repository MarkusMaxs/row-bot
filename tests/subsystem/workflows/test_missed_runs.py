"""N9: a scheduled time that fell while Row-Bot was closed is skipped (the default, and what every
existing workflow keeps), or runs once when Row-Bot starts: only the latest such time, never a
backlog, and the run says it ran late. Every clock here is fixed."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tests.fixtures.tasks import fresh_tasks_module

pytestmark = pytest.mark.subsystem

UTC = timezone.utc
NOW = datetime(2026, 10, 22, 11, 0, tzinfo=UTC)  # 12:00 in London
CLOSED = NOW - timedelta(days=3)  # Row-Bot last ran three days ago.


@pytest.fixture
def tasks(tmp_path, reload_for_data_dir):
    return fresh_tasks_module(tmp_path, reload_for_data_dir)


def _local(moment: datetime) -> str:
    """A time as ``last_run`` stores it: the computer's local time, without an offset."""
    return moment.astimezone().replace(tzinfo=None).isoformat()


def _workflow(tasks, name: str, *, missed_runs: str = "skip", last_run: datetime | None = CLOSED,
              schedule: str = "daily:09:00") -> str:
    task_id = tasks.create_task(name, prompts=["Write the digest"], schedule=schedule,
                                timezone="Europe/London", missed_runs=missed_runs)
    if last_run is not None:
        tasks.update_task(task_id, last_run=_local(last_run))
    return task_id


def test_only_the_latest_missed_time_runs_and_only_for_run_once(tasks):
    run_once = _workflow(tasks, "Digest", missed_runs="run_once")
    _workflow(tasks, "Skipped digest")
    fired: list[tuple[str, str]] = []

    started = tasks.run_missed_runs(CLOSED, NOW, fire=lambda task_id, late_for: fired.append((task_id, late_for)))

    # Three 09:00s fell while it was closed; only today's runs, once.
    assert fired == started == [(run_once, "2026-10-22T09:00:00+01:00")]


def test_nothing_runs_when_no_scheduled_time_fell_while_closed(tasks):
    _workflow(tasks, "Digest", missed_runs="run_once", last_run=NOW - timedelta(hours=3))

    assert tasks.run_missed_runs(NOW - timedelta(hours=1), NOW, fire=lambda *_: pytest.fail("ran")) == []


def test_a_time_that_already_ran_is_not_run_again(tasks):
    ran_today = NOW.replace(hour=8, minute=0, second=5)  # 09:00:05 in London
    _workflow(tasks, "Digest", missed_runs="run_once", last_run=ran_today)

    assert tasks.run_missed_runs(CLOSED, NOW, fire=lambda *_: pytest.fail("ran twice")) == []


def test_a_first_start_that_cant_tell_what_was_missed_runs_nothing(tasks):
    _workflow(tasks, "Digest", missed_runs="run_once")

    assert tasks.run_missed_runs(None, NOW, fire=lambda *_: pytest.fail("ran")) == []


def test_an_interval_schedule_runs_once_for_its_latest_missed_time(tasks):
    task_id = _workflow(tasks, "Every 4 hours", missed_runs="run_once", schedule="interval:4",
                        last_run=CLOSED - timedelta(hours=1))
    fired: list[tuple[str, str]] = []

    tasks.run_missed_runs(CLOSED, NOW, fire=lambda *call: fired.append(call))

    [(fired_id, late_for)] = fired
    assert fired_id == task_id
    assert NOW - timedelta(hours=4) < datetime.fromisoformat(late_for) <= NOW


def test_existing_workflows_keep_skip(tasks):
    conn = tasks._get_conn()
    try:
        conn.execute("INSERT INTO tasks (id,name,prompts,schedule,created_at,enabled) "
                     "VALUES ('old','Old digest','[\"Go\"]','daily:09:00','2026-01-01',1)")
        conn.commit()
    finally:
        conn.close()

    assert tasks.get_task("old")["missed_runs"] == "skip"
    assert tasks.run_missed_runs(CLOSED, NOW, fire=lambda *_: pytest.fail("ran")) == []


def test_the_late_run_says_it_ran_late(tasks, monkeypatch):
    task_id = tasks.create_task("Stretch", notify_only=True, notify_label="Stand up and stretch",
                                schedule="daily:09:00", timezone="Europe/London", missed_runs="run_once")
    from row_bot import notifications

    monkeypatch.setattr(notifications, "notify", lambda **_kwargs: None)
    monkeypatch.setattr(tasks, "_deliver_to_channels", lambda *_a, **_k: ("not_configured", ""))

    tasks._on_task_fire(task_id, late_for="2026-10-22T09:00:00+01:00")

    from row_bot.application.task_execution import list_task_runs

    [run] = list_task_runs(task_id).items
    assert (run.status, run.late_for) == ("completed", "2026-10-22T09:00:00+01:00")


def test_scheduler_start_runs_what_was_missed_once_and_notes_it_is_running(tasks, monkeypatch):
    task_id = _workflow(tasks, "Digest", missed_runs="run_once")
    tasks.note_scheduler_alive(CLOSED)
    fired: list[tuple[str, str]] = []
    monkeypatch.setattr(tasks, "_get_scheduler", lambda: None)
    monkeypatch.setattr(tasks, "sync_all_jobs", lambda: None)
    monkeypatch.setattr(tasks, "start_approval_monitor", lambda: None)
    monkeypatch.setattr(tasks, "_fire_late", lambda *call: fired.append(call))

    tasks.start_task_scheduler()
    tasks.start_task_scheduler()

    assert [fired_id for fired_id, _late in fired] == [task_id]
    assert tasks.scheduler_seen_at() > CLOSED
